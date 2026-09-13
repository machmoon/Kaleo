"""``service/tts.py`` and the ``/speak`` route, entirely offline.

Named ``test_tts_speech.py`` rather than ``test_tts.py`` for no reason beyond
distinctness -- ``scripts/check_docs.py`` keys per-module test counts by file
basename, so a basename already claimed elsewhere would be silently folded
into another module's count. Nothing here loads a model, opens a socket or
runs ``say``: every engine used is either a fake or one whose ``status()``
reports it unavailable, which is the state the suite must pass in.
"""

from __future__ import annotations

import io
import json
import struct
import wave

import pytest

from service import tts
from service.app import Handler

# --------------------------------------------------------------------------
# Request validation
# --------------------------------------------------------------------------


def test_a_valid_body_becomes_a_request():
    req = tts.speak_request(
        {"text": "  Placement is done.  ", "voice": "af_heart", "speed": 1.25}
    )
    assert req.text == "Placement is done."
    assert req.voice == "af_heart"
    assert req.speed == 1.25
    assert req.audio_format == "wav"


@pytest.mark.parametrize(
    "payload, fragment",
    [
        ([], "JSON object"),
        ({}, "'text' is required"),
        ({"text": "   "}, "nothing to say"),
        ({"text": "x" * (tts.MAX_TEXT_CHARS + 1)}, "the limit is"),
        ({"text": "hi", "voice": "../../etc/passwd"}, "'voice' must be"),
        ({"text": "hi", "voice": ""}, "'voice' must be"),
        ({"text": "hi", "engine": 7}, "'engine' must be a string"),
        ({"text": "hi", "format": "mp3"}, "'format' must be one of"),
        ({"text": "hi", "speed": "fast"}, "'speed' must be a number"),
        ({"text": "hi", "speed": True}, "'speed' must be a number"),
        ({"text": "hi", "speed": 9.0}, "between 0.5 and 2.0"),
    ],
)
def test_every_bad_field_is_refused_by_name(payload, fragment):
    """Each refusal says which field and what would fix it -- never a
    generic 'invalid request', which sends nobody anywhere."""
    with pytest.raises(ValueError) as excinfo:
        tts.speak_request(payload)
    assert fragment in str(excinfo.value)


def test_an_overlong_text_is_refused_rather_than_truncated():
    """The alternative -- speaking the first 1200 characters -- ends the
    readback mid-word, which a listener cannot distinguish from Ada deciding
    there was nothing more to say."""
    with pytest.raises(ValueError) as excinfo:
        tts.speak_request({"text": "V bus. " * 400})
    assert "Split it into separate calls" in str(excinfo.value)


# --------------------------------------------------------------------------
# Framing
# --------------------------------------------------------------------------


def test_a_known_length_wav_header_reparses_with_the_stdlib():
    """The header is hand-packed, so it is checked against a reader that
    shares none of its code -- the independent-math discipline
    ``test_kicad.py`` uses for overlap, applied to bytes."""
    pcm = struct.pack("<400h", *([1000, -1000] * 200))
    blob = tts.wav_header(24_000, data_bytes=len(pcm)) + pcm
    with wave.open(io.BytesIO(blob), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == 24_000
        assert handle.readframes(400) == pcm


def test_the_streaming_header_declares_unknown_not_zero():
    """A truthful zero makes every player report a zero-length file and play
    nothing -- the quiet empty body in audio form. 0xFFFFFFFF is the
    'read until the connection closes' convention."""
    header = tts.wav_header(24_000)
    assert len(header) == 44
    (riff,) = struct.unpack("<I", header[4:8])
    (data,) = struct.unpack("<I", header[40:44])
    assert riff == 0xFFFFFFFF
    assert data == 0xFFFFFFFF
    assert header[:4] == b"RIFF" and header[8:12] == b"WAVE"


def test_pcm_conversion_clips_rather_than_wraps():
    """A sample past full scale that wrapped would become a full-amplitude
    click at the opposite polarity: audible, and blamed on the model."""
    frames = tts._floats_to_pcm16([0.0, 1.0, -1.0, 4.2, -4.2])
    assert struct.unpack("<5h", frames) == (0, 32767, -32767, 32767, -32767)


def test_pcm_conversion_agrees_whatever_it_is_handed():
    """The fast path takes a numpy array, the fallback a plain iterable, and
    the two must not disagree about the bytes: a silent divergence would show
    up as one engine sounding subtly different from another."""
    values = [0.0, 0.5, -0.5, 0.25, -0.75, 1.0, -1.0]
    from_list = tts._floats_to_pcm16(values)
    np = pytest.importorskip("numpy")
    from_array = tts._floats_to_pcm16(np.array(values, dtype=np.float32))
    assert from_list == from_array
    assert len(from_list) == 2 * len(values)


def test_pcm_conversion_of_nothing_is_no_bytes():
    """Not an exception and not a stray sample: an empty chunk is legitimate
    mid-stream and must simply contribute nothing."""
    assert tts._floats_to_pcm16([]) == b""


def test_sentences_split_on_boundaries_and_absorb_short_tails():
    readback = (
        "Placement is done. Fifteen parts, no overlaps. Two nets are "
        "unrouted: net zero device pin seven, and V bus."
    )
    chunks = tts.sentences(readback)
    assert len(chunks) >= 2
    assert chunks[0].startswith("Placement is done")
    # Every chunk AFTER the first is a whole thought: no stranded fragment
    # spoken with no preceding context. The first is exempt by design and has
    # its own test below -- it is the time-to-first-audio lever.
    assert all(len(c) >= 24 for c in chunks[1:])
    # Splitting loses no words.
    assert " ".join(chunks).split() == readback.split()


def test_the_first_chunk_is_kept_short_because_it_is_the_latency_lever():
    """Measured with Kokoro fp32 on an M4: "Placement is done." synthesizes in
    ~810 ms, the merged "Placement is done. Fifteen parts, no overlaps." in
    ~1140 ms. The listener waits for the first chunk and nothing else, and the
    real-time factor is below one, so chunk two is ready before chunk one has
    finished playing -- speaking sooner costs nothing later."""
    chunks = tts.sentences(
        "Placement is done. Fifteen parts, no overlaps. Two nets are unrouted."
    )
    assert chunks[0] == "Placement is done."
    # The head is split off; the tail still obeys the merge rule that keeps
    # later chunks whole ("Two nets are unrouted." is under min_chars and so
    # joins its predecessor rather than being stranded).
    assert chunks[1:] == ["Fifteen parts, no overlaps. Two nets are unrouted."]


def test_a_first_chunk_too_short_to_be_worth_a_call_is_merged_anyway():
    """Below ~a half second of speech the fixed per-call cost dominates, so
    splitting buys nothing and spends an extra synthesis."""
    chunks = tts.sentences("Done. Fifteen parts were placed with no overlaps.")
    assert chunks[0].startswith("Done. Fifteen parts")


def test_a_single_sentence_is_never_split_into_a_head_and_nothing():
    assert tts.sentences("Placement is done.") == ["Placement is done."]


def test_sentences_of_nothing_is_empty_not_a_blank_chunk():
    assert tts.sentences("   ") == []


def test_a_colon_never_severs_a_list_of_nets_from_its_introduction():
    """The defect this repo's own vocabulary produces and no TTS benchmark
    measures. Splitting at the colon gives "Two nets are unrouted:" a falling
    terminal contour and restarts the net names as a fresh sentence -- the
    clipped list-reading delivery that makes technical readback sound
    robotic."""
    chunks = tts.sentences(
        "Routing finished. Two are left as ratsnest: V bus, and net zero "
        "device pin seven."
    )
    listing = [c for c in chunks if "ratsnest" in c]
    assert len(listing) == 1
    # The introduction and every net it introduces are spoken as one breath.
    assert "V bus" in listing[0]
    assert "net zero device pin seven" in listing[0]
    assert not any(c.endswith(":") for c in chunks)


def test_a_decimal_is_never_split_into_two_utterances():
    """"18.5 mm" read as "eighteen" then "five millimetres" is a wrong
    number spoken confidently, which is worse than no readback."""
    chunks = tts.sentences("The board is 18.5 by 18.0 millimetres. No overlaps.")
    assert all("18.5" not in c or "18.5 by" in c for c in chunks)
    assert not any(c.endswith("18.") for c in chunks)


def test_a_lone_capitalised_ref_does_not_start_a_new_breath():
    """A split needs a terminal stop, not merely a capital: 'V bus' and 'U1'
    appear mid-sentence constantly in this vocabulary."""
    chunks = tts.sentences("V bus reaches U1 and R3 without a via.")
    assert chunks == ["V bus reaches U1 and R3 without a via."]


# --------------------------------------------------------------------------
# The hosted engine: allowlist, masking, and what an error may carry
# --------------------------------------------------------------------------


def test_the_allowlist_is_exact_not_a_suffix():
    """A suffix test would wave through api.elevenlabs.io.evil.example, which
    is the whole reason billing/googleapps enforce an exact match."""
    tts.ensure_elevenlabs_url("https://api.elevenlabs.io/v1/text-to-speech/x/stream")
    for bad in (
        "https://api.elevenlabs.io.evil.example/v1/x",
        "https://evil.example/api.elevenlabs.io",
        "http://api.elevenlabs.io/v1/x",
    ):
        with pytest.raises(tts.TtsError):
            tts.ensure_elevenlabs_url(bad)


def test_the_allowlist_holds_for_a_fake_transport_too():
    """Enforced in ``HttpRequest.__post_init__``, not inside the real
    transport, so a test double cannot construct a request the production
    path would have refused."""
    with pytest.raises(tts.TtsError):
        tts.HttpRequest(url="https://evil.example/v1/text-to-speech/x/stream")


def test_a_request_repr_never_shows_the_key():
    """This object is in the frame of every transport failure."""
    req = tts.HttpRequest(
        url="https://api.elevenlabs.io/v1/text-to-speech/v/stream",
        headers={"xi-api-key": "sk-super-secret-value", "Accept": "audio/pcm"},
    )
    text = repr(req)
    assert "sk-super-secret-value" not in text
    assert "<set, 21 chars>" in text
    assert "audio/pcm" in text


def test_the_mask_shows_no_prefix_and_no_tail():
    """A tail is enough to confirm a guess."""
    masked = tts.mask_key("sk-abcdef123456")
    assert masked == "<set, 15 chars>"
    assert "abcdef" not in masked and "3456" not in masked
    assert tts.mask_key(None) == "<unset>"
    assert tts.mask_key("") == "<unset>"


def _recorded(chunks, *, status=200):
    """A transport that records the request and replays fixed frames."""
    seen: list[tts.HttpRequest] = []

    def transport(request: tts.HttpRequest) -> tts.HttpResponse:
        seen.append(request)
        return tts.HttpResponse(status=status, chunks=iter(chunks))

    return transport, seen


def test_the_hosted_engine_asks_for_raw_pcm_and_the_flash_model(monkeypatch):
    """mp3 cannot be played until enough of the file exists to decode, which
    is the current webview bug. pcm_24000 *is* the frame contract."""
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-test-key")
    monkeypatch.delenv("ELEVENLABS_VOICE_ID", raising=False)
    transport, seen = _recorded([b"\x01\x02", b"\x03\x04"])
    engine = tts.ElevenLabsEngine(transport=transport)

    frames = b"".join(engine.synthesize(tts.speak_request({"text": "V bus."})))

    assert frames == b"\x01\x02\x03\x04"
    (request,) = seen
    assert "output_format=pcm_24000" in request.url
    assert request.url.endswith("/stream?output_format=pcm_24000")
    assert request.headers["xi-api-key"] == "sk-test-key"
    body = json.loads(request.body)
    assert body["model_id"] == tts.ELEVENLABS_MODEL_ID == "eleven_flash_v2_5"
    assert body["text"] == "V bus."


def test_the_hosted_engine_refuses_a_200_with_no_audio(monkeypatch):
    """An empty 200 is exactly the failure a listener cannot distinguish from
    Ada having nothing to say. It is named, not passed on as silence."""
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-test-key")
    transport, _ = _recorded([b"", b""])
    engine = tts.ElevenLabsEngine(transport=transport)
    with pytest.raises(tts.TtsFailed) as excinfo:
        list(engine.synthesize(tts.speak_request({"text": "V bus."})))
    assert "empty body" in str(excinfo.value)


def test_the_hosted_engine_reports_a_non_200_rather_than_streaming_it(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-test-key")
    transport, _ = _recorded([b"{\"detail\": \"nope\"}"], status=401)
    engine = tts.ElevenLabsEngine(transport=transport)
    with pytest.raises(tts.TtsFailed) as excinfo:
        list(engine.synthesize(tts.speak_request({"text": "V bus."})))
    assert "401" in str(excinfo.value)


def test_with_no_key_the_hosted_engine_is_unconfigured_not_broken(
    monkeypatch, tmp_path
):
    """'unconfigured' and 'unavailable' are different facts and stay
    different words -- the /integrations vocabulary."""
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setenv("KALEO_VOICE_ENV", str(tmp_path / "absent.env"))
    status = tts.ElevenLabsEngine().status()
    assert status.state == "unconfigured"
    assert "ELEVENLABS_API_KEY" in status.hint


def test_the_key_is_read_from_voice_env_when_the_environment_has_none(
    monkeypatch, tmp_path
):
    """The user is told to put it in ~/.kaleo/voice.env, so the one route
    that needs it must actually look there."""
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    env = tmp_path / "voice.env"
    env.write_text("# a comment\nELEVENLABS_API_KEY='sk-from-file'\n")
    monkeypatch.setenv("KALEO_VOICE_ENV", str(env))
    assert tts._elevenlabs_key() == "sk-from-file"
    assert tts.ElevenLabsEngine().status().state == "ready"


def test_the_environment_wins_over_the_file(monkeypatch, tmp_path):
    env = tmp_path / "voice.env"
    env.write_text("ELEVENLABS_API_KEY=sk-from-file\n")
    monkeypatch.setenv("KALEO_VOICE_ENV", str(env))
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-from-env")
    assert tts._elevenlabs_key() == "sk-from-env"


def test_a_ready_status_never_carries_the_key_itself(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-super-secret-value")
    status = tts.ElevenLabsEngine().status()
    assert status.state == "ready"
    assert "sk-super-secret-value" not in json.dumps(status.as_dict())
    assert "<set, 21 chars>" in status.detail


# --------------------------------------------------------------------------
# The local engine's absence is a sentence, not a crash
# --------------------------------------------------------------------------


def test_kokoro_without_its_package_or_weights_says_exactly_what_is_missing(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("KALEO_VOICE_HOME", str(tmp_path))
    status = tts.KokoroEngine().status()
    assert status.state == "unavailable"
    # Whichever is missing first, the hint names both halves of the fix.
    assert "kokoro-onnx" in status.hint
    assert str(tmp_path) in status.hint


def test_kokoro_refuses_to_speak_rather_than_returning_silence(monkeypatch, tmp_path):
    monkeypatch.setenv("KALEO_VOICE_HOME", str(tmp_path))
    engine = tts.KokoroEngine()
    with pytest.raises(tts.TtsUnavailable) as excinfo:
        list(engine.synthesize(tts.speak_request({"text": "V bus."})))
    assert "kokoro cannot speak" in str(excinfo.value)


def test_the_loaded_graph_is_shared_across_engine_instances(monkeypatch, tmp_path):
    """The regression that cost 3.4 s on **every** request.

    ``build_engines`` runs per request, so an instance-scoped session cache
    caches nothing and the 310 MB ONNX graph was reloaded every single call.
    The mistake hid behind a true statement -- constructing an engine really
    is free, the expensive part is the first ``synthesize`` after it -- which
    is the same shape as known issue 5's per-request Firestore client.
    """
    model = tmp_path / "model.onnx"
    voices = tmp_path / "voices.bin"
    model.write_bytes(b"not really a graph")
    voices.write_bytes(b"not really voices")
    monkeypatch.setenv("KALEO_KOKORO_MODEL", str(model))
    monkeypatch.setenv("KALEO_KOKORO_VOICES", str(voices))
    monkeypatch.setattr(tts, "_SESSIONS", {})

    loads = []

    class _FakeKokoro:
        def __init__(self, model_path, voices_path):
            loads.append((model_path, voices_path))

    monkeypatch.setitem(
        __import__("sys").modules,
        "kokoro_onnx",
        type("m", (), {"Kokoro": _FakeKokoro}),
    )

    # Two separate engines, as two requests would build.
    first = tts.KokoroEngine()._load()
    second = tts.KokoroEngine()._load()
    assert first is second
    assert len(loads) == 1, "the graph must be loaded once per process, not per request"


def test_the_weights_are_looked_for_outside_the_checkout(monkeypatch, tmp_path):
    """A 90-330 MB model file must never be in a position to need
    .gitignore's protection."""
    monkeypatch.delenv("KALEO_KOKORO_MODEL", raising=False)
    monkeypatch.delenv("KALEO_KOKORO_VOICES", raising=False)
    monkeypatch.setenv("KALEO_VOICE_HOME", str(tmp_path / "voices"))
    model, voices = tts.kokoro_paths()
    assert model.name == "model.onnx"
    assert voices.name == "voices.bin"
    assert tmp_path in model.parents


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------


class _FakeEngine:
    """A ready engine that speaks a fixed frame. Never loads anything."""

    def __init__(self, name="fake", frames=(b"\x00\x01" * 8,), state="ready"):
        self.name = name
        self.sample_rate = 24_000
        self._frames = frames
        self._state = state
        self.calls: list[tts.SpeechRequest] = []

    def status(self):
        return tts.EngineStatus(self.name, self._state, f"{self.name} is {self._state}")

    def synthesize(self, request):
        self.calls.append(request)
        yield from self._frames


def test_an_explicitly_named_engine_that_is_not_ready_is_refused_not_swapped():
    """A silent downgrade to a different voice is a bug report nobody can
    reproduce: the caller asked for one engine and is entitled to know."""
    engines = {
        "kokoro": _FakeEngine("kokoro", state="unavailable"),
        "elevenlabs": _FakeEngine("elevenlabs"),
    }
    request = tts.speak_request({"text": "V bus.", "engine": "kokoro"})
    with pytest.raises(tts.TtsUnavailable) as excinfo:
        tts.resolve_engine(engines, request)
    assert "kokoro" in str(excinfo.value) and "unavailable" in str(excinfo.value)


def test_an_unknown_engine_name_is_a_caller_mistake():
    engines = {"kokoro": _FakeEngine("kokoro")}
    with pytest.raises(ValueError) as excinfo:
        tts.resolve_engine(engines, tts.speak_request({"text": "hi", "engine": "acme"}))
    assert "unknown engine" in str(excinfo.value)


def test_the_default_order_prefers_the_local_apache_licensed_engine(monkeypatch):
    """No key, no per-word cost, and the only shortlisted model whose weights
    carry no commercial-use question."""
    monkeypatch.delenv("KALEO_TTS_ENGINE", raising=False)
    assert tts.DEFAULT_ENGINE_ORDER == ("kokoro", "elevenlabs")
    engines = {"kokoro": _FakeEngine("kokoro"), "elevenlabs": _FakeEngine("elevenlabs")}
    picked = tts.resolve_engine(engines, tts.speak_request({"text": "hi"}))
    assert picked.name == "kokoro"


def test_the_system_voice_is_not_in_the_default_order(monkeypatch):
    """It is slower to first audio than the webview's own speechSynthesis,
    so routing the fallback through it would be a regression dressed as an
    upgrade. Reachable only by name."""
    monkeypatch.delenv("KALEO_TTS_ENGINE", raising=False)
    assert "system" not in tts.DEFAULT_ENGINE_ORDER
    engines = {
        "kokoro": _FakeEngine("kokoro", state="unavailable"),
        "elevenlabs": _FakeEngine("elevenlabs", state="unconfigured"),
        "system": _FakeEngine("system"),
    }
    with pytest.raises(tts.TtsUnavailable):
        tts.resolve_engine(engines, tts.speak_request({"text": "hi"}))


def test_the_env_var_can_name_a_preference_ladder(monkeypatch):
    monkeypatch.setenv("KALEO_TTS_ENGINE", "elevenlabs, kokoro")
    engines = {"kokoro": _FakeEngine("kokoro"), "elevenlabs": _FakeEngine("elevenlabs")}
    picked = tts.resolve_engine(engines, tts.speak_request({"text": "hi"}))
    assert picked.name == "elevenlabs"


def test_with_nothing_configured_the_refusal_names_every_reason(monkeypatch):
    monkeypatch.delenv("KALEO_TTS_ENGINE", raising=False)
    engines = {
        "kokoro": _FakeEngine("kokoro", state="unavailable"),
        "elevenlabs": _FakeEngine("elevenlabs", state="unconfigured"),
    }
    with pytest.raises(tts.TtsUnavailable) as excinfo:
        tts.resolve_engine(engines, tts.speak_request({"text": "hi"}))
    message = str(excinfo.value)
    assert "kokoro is unavailable" in message
    assert "elevenlabs is unconfigured" in message
    # And it names what the client should do instead, rather than leaving it
    # to guess that silence was intended.
    assert "speechSynthesis" in message


def test_synthesize_pulls_the_first_frame_eagerly():
    """An engine that fails on its first frame must produce an HTTP error, not
    a 200 with a truncated body: once bytes are on the wire this HTTP/1.0
    server has no way left to signal a failure."""

    class _FailsImmediately(_FakeEngine):
        def synthesize(self, request):
            raise tts.TtsFailed("the model did not load")
            yield  # pragma: no cover - unreachable, keeps this a generator

    engines = {"kokoro": _FailsImmediately("kokoro")}
    with pytest.raises(tts.TtsFailed):
        tts.synthesize(engines, tts.speak_request({"text": "hi", "engine": "kokoro"}))


def test_synthesize_refuses_an_engine_that_yields_nothing():
    engines = {"kokoro": _FakeEngine("kokoro", frames=())}
    with pytest.raises(tts.TtsFailed) as excinfo:
        tts.synthesize(engines, tts.speak_request({"text": "hi", "engine": "kokoro"}))
    assert "no audio at all" in str(excinfo.value)


def test_synthesize_replays_the_eagerly_pulled_frame():
    """The frame pulled to prove the engine works must still reach the
    listener -- dropping it would clip the first ~40 ms of every utterance."""
    engines = {"kokoro": _FakeEngine("kokoro", frames=(b"AB", b"CD", b"EF"))}
    engine, stream = tts.synthesize(
        engines, tts.speak_request({"text": "hi", "engine": "kokoro"})
    )
    assert engine.name == "kokoro"
    assert b"".join(stream) == b"ABCDEF"


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------


def test_the_report_names_the_selected_engine_and_why_not_the_others():
    engines = {
        "kokoro": _FakeEngine("kokoro", state="unavailable"),
        "elevenlabs": _FakeEngine("elevenlabs"),
    }
    report = tts.speak_report(engines)
    assert report["selected"] == "elevenlabs"
    states = {e["name"]: e["state"] for e in report["engines"]}
    assert states == {"kokoro": "unavailable", "elevenlabs": "ready"}
    assert report["fallback"] is None


def test_the_report_states_the_fallback_when_nothing_is_ready():
    engines = {"kokoro": _FakeEngine("kokoro", state="unavailable")}
    report = tts.speak_report(engines)
    assert report["selected"] is None
    assert "speechSynthesis" in report["fallback"]


def test_a_probe_that_raises_becomes_one_engine_state_not_a_failed_report():
    """One broken engine must not hide the ones that were fine -- the rule
    ``service/integrations.py`` states for the same reason."""

    class _Explodes(_FakeEngine):
        def status(self):
            raise RuntimeError("boom")

    engines = {"kokoro": _Explodes("kokoro"), "elevenlabs": _FakeEngine("elevenlabs")}
    report = tts.speak_report(engines)
    states = {e["name"]: e["state"] for e in report["engines"]}
    assert states["kokoro"] == "unavailable"
    assert states["elevenlabs"] == "ready"
    assert report["selected"] == "elevenlabs"


def test_the_report_declares_each_licence():
    """The licence is the reason the default is what it is, so it travels with
    the status rather than living only in a comment."""
    report = tts.speak_report(tts.build_engines())
    licences = {e["name"]: e["licence"] for e in report["engines"]}
    assert "Apache-2.0" in licences["kokoro"]
    assert "billed per character" in licences["elevenlabs"]
    assert "Apple" in licences["system"]


def test_build_engines_loads_nothing_and_calls_nothing():
    """Safe to call per request: no model is read and no socket is opened."""
    engines = tts.build_engines()
    assert set(engines) == {"kokoro", "elevenlabs", "system"}
    for engine in engines.values():
        assert engine.status().state in {"ready", "unconfigured", "unavailable"}


# --------------------------------------------------------------------------
# The route
# --------------------------------------------------------------------------


class _Wire(io.BytesIO):
    def flush(self) -> None:  # pragma: no cover - trivial
        pass


def _drive(method: str, path: str, body: dict | None, engines: dict):
    """One request through the real Handler with its sockets faked."""
    handler = Handler.__new__(Handler)
    raw = json.dumps(body).encode() if body is not None else b""
    handler.rfile = io.BytesIO(raw)
    handler.wfile = _Wire()
    handler.path = path
    handler.request_version = "HTTP/1.0"
    handler.close_connection = False
    handler.headers = {
        "Content-Type": "application/json",
        "Content-Length": str(len(raw)),
    }
    handler.tts_engines_factory = lambda: engines
    handler.log_request = lambda *a, **k: None
    handler.log_error = lambda *a, **k: None
    if method == "GET":
        handler._speak_report()
    else:
        handler._speak()
    return handler.wfile.getvalue()


def _split(raw: bytes) -> tuple[str, bytes]:
    head, _, rest = raw.partition(b"\r\n\r\n")
    return head.decode("latin-1"), rest


def test_the_route_streams_a_wav_and_names_the_engine_in_a_header():
    engines = {"kokoro": _FakeEngine("kokoro", frames=(b"AB", b"CD"))}
    head, body = _split(_drive("POST", "/speak", {"text": "V bus."}, engines))
    assert "200" in head
    assert "Content-Type: audio/wav" in head
    # Which voice answered, before a frame is played.
    assert "X-Kaleo-Tts-Engine: kokoro" in head
    assert "X-Kaleo-Sample-Rate: 24000" in head
    assert "Cache-Control: no-store" in head
    # A streaming body: no length, and the WAV header precedes the frames.
    assert "Content-Length" not in head
    assert body == tts.wav_header(24_000) + b"ABCD"


def test_the_route_can_hand_back_raw_frames_for_web_audio():
    engines = {"kokoro": _FakeEngine("kokoro", frames=(b"AB", b"CD"))}
    head, body = _split(
        _drive("POST", "/speak", {"text": "V bus.", "format": "pcm16"}, engines)
    )
    assert "Content-Type: audio/L16" in head
    assert body == b"ABCD"  # no container at all


def test_a_bad_field_is_a_400_with_its_text():
    engines = {"kokoro": _FakeEngine("kokoro")}
    head, body = _split(_drive("POST", "/speak", {"text": ""}, engines))
    assert "400" in head
    assert "nothing to say" in json.loads(body)["error"]


def test_nothing_configured_is_a_503_naming_the_client_fallback():
    """Not a 500, and not a silent 200 with an empty body: the client's
    correct response is its own speechSynthesis, and the body says so."""
    engines = {"kokoro": _FakeEngine("kokoro", state="unavailable")}
    head, body = _split(_drive("POST", "/speak", {"text": "V bus."}, engines))
    assert "503" in head
    payload = json.loads(body)
    assert payload["fallback"] == "client speechSynthesis"
    assert "kokoro is unavailable" in payload["error"]


def test_a_configured_engine_that_fails_is_a_502_not_a_500():
    """Upstream is down, not the caller's fault -- the call _error_response
    makes for ModelError, made here for a speech host."""

    class _Fails(_FakeEngine):
        def synthesize(self, request):
            raise tts.TtsFailed("the speech host refused the request: HTTP 401")
            yield  # pragma: no cover

    engines = {"kokoro": _Fails("kokoro")}
    head, body = _split(_drive("POST", "/speak", {"text": "V bus."}, engines))
    assert "502" in head
    assert "401" in json.loads(body)["error"]


def test_the_route_never_leaks_a_key_into_a_failure_body(monkeypatch):
    """The one failure that has a key anywhere near it."""
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-super-secret-value")

    def transport(request):
        raise tts.TtsFailed("the speech host could not be reached: timed out")

    engines = {"elevenlabs": tts.ElevenLabsEngine(transport=transport)}
    head, body = _split(
        _drive("POST", "/speak", {"text": "V bus.", "engine": "elevenlabs"}, engines)
    )
    assert "502" in head
    raw = body.decode()
    assert "sk-super-secret-value" not in raw
    assert "api.elevenlabs.io" not in raw  # no URL either


def test_the_get_answers_a_report_and_never_500s():
    class _Explodes(_FakeEngine):
        def status(self):
            raise RuntimeError("boom")

    engines = {"kokoro": _Explodes("kokoro"), "elevenlabs": _FakeEngine("elevenlabs")}
    head, body = _split(_drive("GET", "/speak", None, engines))
    assert "200" in head
    assert "Cache-Control: no-store" in head
    report = json.loads(body)
    assert report["selected"] == "elevenlabs"
    assert report["formats"] == list(tts.AUDIO_FORMATS)

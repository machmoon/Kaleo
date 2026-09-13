"""transcribe_audio: the audio rides the existing Document seam.

Offline tests drive :class:`ScriptedModel`, the same way every other agent
stage is tested; one live test at the bottom follows test_live_model.py's
API-key gate and actually sends Gemini a generated WAV.
"""

from __future__ import annotations

import io
import math
import os
import struct
import wave

import pytest
from silkscreen.agents.model import CHEAP_MODEL, GeminiModel, ModelError, ScriptedModel
from silkscreen.agents.transcribe import transcribe_audio

requires_api_key = pytest.mark.skipif(
    not os.getenv("GOOGLE_API_KEY"),
    reason="live Gemini test: set GOOGLE_API_KEY to run",
)

#: The marker a service test's scripted model keys on too. Pinning it here
#: means a prompt rewrite that breaks the tests announces itself.
MARKER = "transcribing a spoken request"


def scripted(text: str = "a 3.3 volt LDO board") -> ScriptedModel:
    return ScriptedModel(by_marker={MARKER: text})


def test_returns_the_models_transcript_stripped():
    model = scripted("  a 3.3 volt LDO board \n")
    assert transcribe_audio(model, b"RIFFaudio", "audio/wav") == "a 3.3 volt LDO board"


def test_audio_travels_as_an_inline_document():
    model = scripted()
    transcribe_audio(model, b"\x00\x01\x02", "audio/webm")

    (call,) = model.calls
    (doc,) = call["documents"]
    assert doc.data == b"\x00\x01\x02"
    assert doc.url is None
    assert doc.mime_type == "audio/webm"


def test_prompt_asks_for_transcript_only():
    model = scripted()
    transcribe_audio(model, b"x", "audio/wav")
    prompt = model.calls[0]["prompt"]
    assert "ONLY the transcript" in prompt
    assert "language" not in prompt.lower()


def test_language_hint_reaches_the_prompt_only_when_given():
    model = scripted()
    transcribe_audio(model, b"x", "audio/wav", language="de")
    assert "most likely in this language: de" in model.calls[0]["prompt"]


def test_a_failed_call_raises_rather_than_returning_empty():
    model = ScriptedModel()  # no responses scripted
    with pytest.raises(ModelError):
        transcribe_audio(model, b"x", "audio/wav")


# ------------------------------------------------------------- live Gemini


def sine_wav(seconds: float = 1.0, freq_hz: float = 440.0) -> bytes:
    """A 1-second mono 16-bit 16 kHz sine tone, synthesized with stdlib wave."""
    rate = 16000
    n = int(rate * seconds)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(
            b"".join(
                struct.pack(
                    "<h", int(20000 * math.sin(2 * math.pi * freq_hz * i / rate))
                )
                for i in range(n)
            )
        )
    return buf.getvalue()


@requires_api_key
def test_live_transcribe_accepts_audio_and_answers_text():
    """One cheap-model call with real inline audio.

    A pure tone carries no words, so the *content* of the answer is the
    model's business (the prompt asks for "(inaudible)" in that case); what
    this pins is the transport -- that an audio part with an audio MIME type
    goes through the GeminiModel request path and comes back as text.
    """
    model = GeminiModel(CHEAP_MODEL)
    text = transcribe_audio(model, sine_wav(), "audio/wav")
    assert isinstance(text, str)
    assert text  # transcribe_audio strips; GeminiModel raises on truly empty

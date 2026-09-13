"""Ada's voice: text in, audio frames out, decided in the service.

Why this lives here and not in the webview
------------------------------------------

Every word Ada speaks today is the browser's ``speechSynthesis``, which on
macOS resolves to a *Compact* system voice -- the robot the complaint is
about. Moving synthesis behind an HTTP route buys three things the webview
cannot have:

* **A choice of engine that is not the platform's.** The webview can only
  speak with voices the OS installed. The service can run a real neural model
  or call a hosted one.
* **One seam, three front ends.** The SPA, the desktop overlay and anything
  else that wants Ada's voice ask the same route rather than each re-deriving
  a backend ladder.
* **Keys stay out of the renderer.** An ``ELEVENLABS_API_KEY`` read in the
  webview is a key in a webview; read here it never crosses the process
  boundary, and no error, warning or verdict in this module carries it.

The frame contract
------------------

**Every engine yields signed 16-bit little-endian mono PCM frames** at its own
:attr:`TtsEngine.sample_rate`. That is the one representation, chosen because
it is what the two candidate engines natively produce (Kokoro emits float
samples we quantise once; ElevenLabs will serve ``pcm_24000`` directly) and
because it is the only format a caller can begin playing before the last byte
arrives. ``wav_header`` wraps those frames for a caller that wants a file;
``format="pcm16"`` hands them over raw for a caller feeding Web Audio.

Nothing returns a quiet zero
----------------------------

The ``spice/`` discipline applies with force here, because a TTS failure and
silence are indistinguishable to a listener. An engine that cannot speak
raises a :class:`TtsError` naming the engine, what was missing and the exact
fix; it never yields an empty stream and never falls through to a worse
engine behind the caller's back. When *no* engine is configured the route
answers 503 with that reason, and the client is expected to fall back to its
own ``speechSynthesis`` -- i.e. to today's behaviour, deliberately and
visibly, rather than to silence.

Engine precedence and why ``system`` is not in it
-------------------------------------------------

Default order is :data:`DEFAULT_ENGINE_ORDER` -- Kokoro first (no key, no
per-word cost, Apache-2.0 weights), ElevenLabs second (better, but hosted and
metered). ``system`` (macOS ``say``) is deliberately **excluded from the
default order** and reachable only by naming it: measured on an M4 Mac it
needs ~1.8 s to synthesize one sentence to a file, because ``say`` cannot
write to a pipe (it needs a seekable output) and so cannot stream at all.
That is *slower* than the webview's own ``speechSynthesis``, which starts
speaking in ~50-100 ms. Routing the fallback through here would therefore be
a latency regression dressed as an upgrade. It stays available for
development, for a machine that has Enhanced/Premium voices installed, and so
that the route has something to exercise offline.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import wave
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

__all__ = [
    "ALLOWED_HOSTS",
    "DEFAULT_ENGINE_ORDER",
    "DEFAULT_KOKORO_VOICE",
    "ELEVENLABS_MODEL_ID",
    "AUDIO_FORMATS",
    "MAX_TEXT_CHARS",
    "ElevenLabsEngine",
    "EngineStatus",
    "HttpRequest",
    "HttpResponse",
    "KokoroEngine",
    "SpeechRequest",
    "SystemVoiceEngine",
    "Transport",
    "TtsEngine",
    "TtsError",
    "TtsFailed",
    "TtsUnavailable",
    "build_engines",
    "ensure_elevenlabs_url",
    "kokoro_paths",
    "mask_key",
    "resolve_engine",
    "sentences",
    "speak_request",
    "speak_report",
    "synthesize",
    "urllib_transport",
    "wav_header",
]


# --------------------------------------------------------------------------
# Errors. Two of them, because the caller can act on the difference: nothing
# is configured (fall back to your own voice) versus the configured thing
# broke (say so, do not silently downgrade).
# --------------------------------------------------------------------------


class TtsError(RuntimeError):
    """Base for every refusal in this module. Never carries a key or a URL."""


class TtsUnavailable(TtsError):
    """No engine could speak, and the message says what would make one work.

    Answered as a 503. The honest client response is to fall back to its own
    ``speechSynthesis`` -- which is exactly today's behaviour -- and to say
    out loud that it did.
    """


class TtsFailed(TtsError):
    """A configured engine was asked to speak and could not.

    Answered as a 502. Deliberately *not* an automatic fall-through to a
    lesser engine: a voice that silently changes mid-session is a bug report
    nobody can reproduce.
    """


# --------------------------------------------------------------------------
# Request shape
# --------------------------------------------------------------------------

#: Audio a caller may ask for. ``pcm16`` is the raw frame contract (mono,
#: signed 16-bit little-endian, at the engine's sample rate, reported in the
#: ``X-Kaleo-Sample-Rate`` response header); ``wav`` is those frames behind a
#: RIFF header, for a caller that wants to hand the body to an ``<audio>``
#: element rather than to Web Audio.
AUDIO_FORMATS = ("wav", "pcm16")

#: One spoken turn. Ada reads short status sentences, not documents, and an
#: unbounded body on a metered hosted engine is an unbounded bill. A longer
#: request is refused by name rather than truncated, because a truncated
#: readback that stops mid-net-name is the exact dishonesty this repo's
#: routing/review layers exist to prevent.
MAX_TEXT_CHARS = 1200

#: Kokoro ships 54 voices; ``af_heart`` is its own reference voice and the one
#: the model card demonstrates.
DEFAULT_KOKORO_VOICE = "af_heart"

#: ElevenLabs' lowest-latency model. Their headline "75 ms" is *inference
#: only* and excludes the network by their own documentation; the measured
#: end-to-end figure in the field is ~288 ms P50.
ELEVENLABS_MODEL_ID = "eleven_flash_v2_5"

#: "Rachel", ElevenLabs' default public voice. Overridable per request and by
#: ``ELEVENLABS_VOICE_ID`` so nobody has to edit code to change Ada's voice.
DEFAULT_ELEVENLABS_VOICE = "21m00Tcm4TlvDq8ikWAM"

#: Tried in this order when the caller names no engine. ``system`` is absent
#: on purpose -- see the module docstring.
DEFAULT_ENGINE_ORDER = ("kokoro", "elevenlabs")

_VOICE_RE = re.compile(r"\A[A-Za-z0-9_.-]{1,64}\Z")


@dataclass(frozen=True)
class SpeechRequest:
    """One validated ask. Construct it with :func:`speak_request`."""

    text: str
    voice: str | None = None
    engine: str | None = None
    audio_format: str = "wav"
    speed: float = 1.0


def speak_request(payload: Any) -> SpeechRequest:
    """Validate one ``POST /speak`` body, or refuse it as a ``ValueError``.

    Every failure is a caller-fixable field problem, which is why they are all
    plain ``ValueError``\\ s here: ``service/app.py`` re-raises them as its own
    ``RequestError`` at the route, keeping this module free of any dependency
    on the HTTP layer's taxonomy.
    """
    if not isinstance(payload, dict):
        raise ValueError("body must be a JSON object")

    text = payload.get("text")
    if not isinstance(text, str):
        raise ValueError("'text' is required and must be a string")
    text = text.strip()
    if not text:
        raise ValueError("'text' is empty: there is nothing to say")
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError(
            f"'text' is {len(text)} characters; the limit is {MAX_TEXT_CHARS}. "
            "Split it into separate calls rather than sending a truncated "
            "sentence -- a readback that stops mid-net-name is worse than two "
            "requests"
        )

    voice = payload.get("voice")
    if voice is not None and (
        not isinstance(voice, str) or not _VOICE_RE.match(voice)
    ):
        raise ValueError(
            "'voice' must be 1-64 characters of letters, digits, '.', '_' or '-'"
        )

    engine = payload.get("engine")
    if engine is not None and not isinstance(engine, str):
        raise ValueError("'engine' must be a string")

    audio_format = payload.get("format", "wav")
    if audio_format not in AUDIO_FORMATS:
        raise ValueError(f"'format' must be one of {', '.join(AUDIO_FORMATS)}")

    raw_speed = payload.get("speed", 1.0)
    if isinstance(raw_speed, bool) or not isinstance(raw_speed, (int, float)):
        raise ValueError("'speed' must be a number")
    speed = float(raw_speed)
    if not 0.5 <= speed <= 2.0:
        raise ValueError("'speed' must be between 0.5 and 2.0")

    return SpeechRequest(
        text=text,
        voice=voice,
        engine=engine,
        audio_format=audio_format,
        speed=speed,
    )


# --------------------------------------------------------------------------
# WAV framing
# --------------------------------------------------------------------------


def wav_header(sample_rate: int, *, data_bytes: int | None = None) -> bytes:
    """A 44-byte RIFF/WAVE header for mono signed 16-bit little-endian PCM.

    ``data_bytes`` is the payload length when it is known. When it is not --
    the streaming case, which is the whole point of this route -- the two size
    fields are written as ``0xFFFFFFFF``. That is the conventional "unknown,
    read until the connection closes" marker: every browser, ``afplay`` and
    ``ffmpeg`` accept it, and the alternative (a truthful zero) makes players
    report a zero-length file and play nothing, which would be exactly the
    quiet-empty-body failure this module refuses everywhere else.
    """
    unknown = 0xFFFFFFFF
    if data_bytes is None:
        riff_size = unknown
        payload = unknown
    else:
        payload = data_bytes
        riff_size = 36 + data_bytes
    byte_rate = sample_rate * 2  # mono * 2 bytes/sample
    return b"".join(
        (
            b"RIFF",
            struct.pack("<I", riff_size),
            b"WAVE",
            b"fmt ",
            struct.pack("<I", 16),  # PCM chunk size
            struct.pack("<H", 1),  # format = PCM
            struct.pack("<H", 1),  # channels = mono
            struct.pack("<I", sample_rate),
            struct.pack("<I", byte_rate),
            struct.pack("<H", 2),  # block align
            struct.pack("<H", 16),  # bits per sample
            b"data",
            struct.pack("<I", payload),
        )
    )


#: Split only on a *terminal* stop -- ``.``, ``!``, ``?`` -- followed by
#: whitespace and a capital. Deliberately **not** on ``:`` or ``;``.
#:
#: A colon in this product's vocabulary introduces a list of net names
#: ("Two nets are unrouted: V bus, and net zero device pin seven"), and
#: cutting there is audibly wrong: every neural TTS gives a chunk-final
#: token a falling terminal contour, so the introduction lands as a finished
#: sentence and the net names start as a new one. That is the clipped,
#: list-reading delivery this whole change exists to remove, and it is the
#: kind of defect no TTS benchmark measures -- none of them read structured
#: technical output.
#:
#: Requiring whitespace *and* a following capital also keeps decimals whole:
#: "18.5 mm" has no space after the stop, so it is never a split candidate,
#: and no digit guard is needed (one would wrongly refuse the legitimate
#: split in "...is 18. Two nets are unrouted").
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")


#: A first chunk shorter than this is merged forward anyway. Below roughly a
#: half-second of speech the ~500 ms fixed cost of a synthesis call dominates,
#: so splitting it off buys nothing and spends an extra call.
MIN_FIRST_CHUNK_CHARS = 12


def sentences(
    text: str, *, min_chars: int = 24, first_chunk_short: bool = True
) -> list[str]:
    """Split a readback into speakable chunks, for chunked synthesis.

    Neither Kokoro nor ``say`` accepts streaming text, so time-to-first-audio
    is bounded below by synthesizing the *whole* utterance -- unless the
    utterance is cut at sentence boundaries and each piece synthesized in
    turn, which recovers most of the benefit for a few lines of code.

    Short trailing fragments are glued onto the previous chunk: a chunk of
    ``"and V bus."`` on its own gets neural prosody with no preceding context
    and reads as a new sentence, which is precisely the clipped delivery this
    change exists to remove.

    **The first chunk is deliberately left short** (``first_chunk_short``),
    and that asymmetry is the single largest lever on time-to-first-audio.
    Measured here with Kokoro fp32: "Placement is done." synthesizes in
    ~810 ms, while "Placement is done. Fifteen parts, no overlaps." -- what
    the plain ``min_chars`` merge produced -- takes ~1140 ms for a chunk the
    listener does not need any sooner. Because the real-time factor is below
    one, synthesis of chunk two finishes well inside chunk one's playback, so
    speaking earlier costs nothing later: the stream never starves. Only the
    *first* chunk gets this treatment, because the merge that keeps later
    chunks whole is what protects their prosody, and after the first chunk
    nobody is waiting.
    """
    parts = [p.strip() for p in _SENTENCE_RE.split(text) if p.strip()]
    if not parts:
        return []

    head: list[str] = []
    if first_chunk_short and len(parts) > 1 and len(parts[0]) >= MIN_FIRST_CHUNK_CHARS:
        head = [parts[0]]
        parts = parts[1:]

    merged: list[str] = [parts[0]]
    for part in parts[1:]:
        if len(part) < min_chars or len(merged[-1]) < min_chars:
            merged[-1] = f"{merged[-1]} {part}"
        else:
            merged.append(part)
    return head + merged


# --------------------------------------------------------------------------
# The engine seam
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EngineStatus:
    """What one engine can do right now, and what would make it do more.

    Four states, the ``service/integrations.py`` vocabulary, for the same
    reason: an engine that is merely un-keyed must not look like one that is
    broken, and an un-built one must stay visibly un-built.

    ``ready`` here is a claim about *configuration*, never a live probe --
    nothing in this module synthesizes a test utterance to find out.
    """

    name: str
    state: str  # "ready" | "unconfigured" | "unavailable"
    detail: str
    hint: str = ""
    sample_rate: int = 0
    streaming: bool = False
    licence: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state,
            "detail": self.detail,
            "hint": self.hint,
            "sample_rate": self.sample_rate,
            "streaming": self.streaming,
            "licence": self.licence,
        }


class TtsEngine(Protocol):
    """One way to turn text into PCM16 frames.

    ``synthesize`` is a generator on purpose: the first frame must be able to
    reach the wire before the last one has been computed.
    """

    name: str
    sample_rate: int

    def status(self) -> EngineStatus:
        """Configuration state. Must never raise and never make a call."""
        ...

    def synthesize(self, request: SpeechRequest) -> Iterator[bytes]:
        """PCM16 mono little-endian frames, or a :class:`TtsError`."""
        ...


def _floats_to_pcm16(samples: Iterable[float]) -> bytes:
    """Quantise float samples in [-1, 1] to signed 16-bit little-endian.

    Clipped, not wrapped. A sample past full scale that wraps becomes a loud
    click at the opposite polarity -- audible, and the sort of defect that
    gets blamed on the model rather than on the conversion.

    **Vectorised, because this sits on the latency path.** Three seconds of
    24 kHz audio is 72,000 samples, and the obvious Python loop spends ~228 ms
    of pure interpreter overhead on them -- measured, and about a fifth of the
    time-to-first-audio budget spent on arithmetic that numpy does in 1.5 ms.
    numpy is imported *here* rather than at module scope on purpose:
    ``service/`` is a stdlib-only HTTP surface by convention, and the only
    caller of this function is the Kokoro engine, which cannot run at all
    without numpy (``kokoro-onnx`` depends on it). The loop remains as the
    fallback so the convention holds literally: nothing in this module
    *requires* a third-party import to work.
    """
    try:
        import numpy as np
    except ImportError:
        out = bytearray()
        for value in samples:
            clipped = -1.0 if value < -1.0 else (1.0 if value > 1.0 else float(value))
            out += struct.pack("<h", int(clipped * 32767.0))
        return bytes(out)

    arr = np.asarray(list(samples) if not hasattr(samples, "dtype") else samples)
    if arr.size == 0:
        return b""
    # rint, not truncation: rounding toward zero on every sample is a
    # systematic pull toward silence, which is quantisation distortion rather
    # than the dither-free rounding a listener expects. '<i2' pins the byte
    # order, so a big-endian host cannot silently emit byte-swapped audio.
    scaled = np.rint(np.clip(arr, -1.0, 1.0) * 32767.0)
    return scaled.astype("<i2").tobytes()


# --------------------------------------------------------------------------
# Kokoro-82M, local
# --------------------------------------------------------------------------


def kokoro_paths() -> tuple[Path, Path]:
    """Where the Kokoro ONNX graph and voice pack are looked for.

    Under ``~/.kaleo/`` beside the other machine-local state this product
    already keeps there (``demo/``, ``*.env``, ``voice.env``) -- deliberately
    outside the checkout, because ``.gitignore`` protects a repo from junk and
    a 90-330 MB weight file should never be in a position to need protecting.
    Both are overridable so a shared or pre-seeded cache needs no code change.

    **Provisioning, for whoever hits the hint in :meth:`KokoroEngine.status`.**
    Two files, from the ``kokoro-onnx`` release ``model-files-v1.1``::

        model.onnx   <- kokoro-v1.0.onnx         310 MiB  (fp32 -- use this)
        voices.bin   <- voices-v1.0.bin           27 MiB  (all 54 voices)

    **Take the fp32 weights, not a quantised build.** Measured here on an M4
    (2026-09-07, warm session, same sentence, median of three), the intuition
    that the small file is the fast one is exactly backwards:

    ====================  ========  =====  ===========================
    build                 size      RTF    note
    ====================  ========  =====  ===========================
    int8                  109 MiB   1.52   *slower than real time*
    fp16                  156 MiB   0.81   logs missing-CPU-kernel warnings
    **fp32**              310 MiB   0.77   fastest, and the reference weights
    ====================  ========  =====  ===========================

    onnxruntime's CPU kernels for these quantised graphs fall back to slow
    reference implementations on ARM, so int8 pays a 2x speed penalty to save
    200 MB of disk. Disk is the cheap resource here.

    Nothing in this repo downloads them. That is the refusal
    :class:`KokoroEngine` documents: a first spoken word that silently pulls
    140 MB is a surprise on a metered connection and a hang on a hot path.
    """
    root = Path(os.getenv("KALEO_VOICE_HOME", str(Path.home() / ".kaleo" / "voices")))
    model = os.getenv("KALEO_KOKORO_MODEL") or str(root / "kokoro" / "model.onnx")
    voices = os.getenv("KALEO_KOKORO_VOICES") or str(root / "kokoro" / "voices.bin")
    return Path(model), Path(voices)


#: Loaded Kokoro graphs, keyed by ``(model path, voices path)`` and shared by
#: every request in this process. Module scope is the point -- see
#: :class:`KokoroEngine`. Never evicted: there is one entry per weights file an
#: operator provisioned, which is one.
_SESSIONS: dict[tuple[str, str], Any] = {}
_SESSION_LOCK = threading.Lock()


@dataclass
class KokoroEngine:
    """Kokoro-82M through ``kokoro-onnx``, run in this process.

    **Licence: Apache-2.0 for the code and, unusually, for the weights too.**
    That is the reason it is the default rather than a better-sounding
    non-commercial model: it is the only entry on the shortlist with no
    commercial-use question at all.

    Two things must both be present or this engine reports ``unavailable``
    and says which one is missing:

    * the ``kokoro-onnx`` package (which pulls ``onnxruntime``), and
    * the ONNX graph plus voice pack under :func:`kokoro_paths`.

    Neither is installed by this repo and neither is downloaded on demand.
    That is a deliberate refusal: a service that quietly pulls hundreds of
    megabytes the first time somebody speaks is a surprise on a metered
    connection and a hang on a hot path. Provisioning is an explicit,
    operator-run step, and until it happens this engine says so in words.

    Synthesis is chunked at sentence boundaries via :func:`sentences`, because
    Kokoro takes no streaming text input -- the first sentence reaches the
    wire while the second is still being computed.

    The loaded graph is cached in :data:`_SESSIONS`, at module scope rather
    than on the instance, because ``build_engines`` runs **per request** and an
    instance-scoped cache therefore caches nothing: measured on this machine,
    reloading the graph cost ~1.4-3.4 s on every single call. That is the
    same shape as known issue 5 (a fresh Firestore client per request) and it
    is worth naming, because the mistake hid behind a true statement --
    *constructing* an engine really is free, and the expensive part is the
    first ``synthesize`` after it.
    """

    name: str = "kokoro"
    sample_rate: int = 24_000

    def _missing(self) -> str | None:
        """The first thing standing between here and audio, or None."""
        try:
            import kokoro_onnx  # noqa: F401
        except ImportError:
            return (
                "the 'kokoro-onnx' package is not installed (it brings "
                "onnxruntime with it)"
            )
        model, voices = kokoro_paths()
        if not model.is_file():
            return f"the ONNX graph is not at {model}"
        if not voices.is_file():
            return f"the voice pack is not at {voices}"
        return None

    def status(self) -> EngineStatus:
        try:
            missing = self._missing()
        except Exception as exc:  # pragma: no cover - defensive
            # A probe that blows up becomes this engine's state, never a 500
            # that hides the engines that were fine. `service/integrations.py`
            # made the same call for the same reason.
            return EngineStatus(
                self.name,
                "unavailable",
                f"could not be inspected: {type(exc).__name__}",
                sample_rate=self.sample_rate,
                streaming=True,
                licence="Apache-2.0 (code and weights)",
            )
        model, voices = kokoro_paths()
        if missing is None:
            return EngineStatus(
                self.name,
                "ready",
                "Kokoro-82M loaded from disk, no key and no per-word cost",
                sample_rate=self.sample_rate,
                streaming=True,
                licence="Apache-2.0 (code and weights)",
            )
        return EngineStatus(
            self.name,
            "unavailable",
            f"not usable here: {missing}",
            hint=(
                "install 'kokoro-onnx' and place the Kokoro-82M ONNX graph at "
                f"{model} and the voice pack at {voices}"
            ),
            sample_rate=self.sample_rate,
            streaming=True,
            licence="Apache-2.0 (code and weights)",
        )

    def _load(self) -> Any:
        missing = self._missing()
        if missing is not None:
            raise TtsUnavailable(f"kokoro cannot speak: {missing}")
        model, voices = kokoro_paths()
        key = (str(model), str(voices))
        # Double-checked under the lock: two requests arriving together must
        # load the graph once, not twice. Loading it twice is not merely slow,
        # it is two copies of a 310 MiB graph resident on a laptop that is
        # also running CP-SAT.
        session = _SESSIONS.get(key)
        if session is not None:
            return session
        with _SESSION_LOCK:
            session = _SESSIONS.get(key)
            if session is not None:
                return session
            from kokoro_onnx import Kokoro  # type: ignore[import-not-found]

            try:
                session = Kokoro(*key)
            except Exception as exc:
                raise TtsFailed(
                    f"kokoro failed to load its model: {type(exc).__name__}: {exc}"
                ) from exc
            _SESSIONS[key] = session
            return session

    def synthesize(self, request: SpeechRequest) -> Iterator[bytes]:
        session = self._load()
        voice = request.voice or os.getenv("KALEO_KOKORO_VOICE") or DEFAULT_KOKORO_VOICE
        spoke = False
        for chunk in sentences(request.text):
            try:
                samples, rate = session.create(
                    chunk, voice=voice, speed=request.speed, lang="en-us"
                )
            except Exception as exc:
                # Named, mid-stream, rather than a short read the caller would
                # hear as Ada trailing off.
                raise TtsFailed(
                    f"kokoro failed on {len(chunk)} characters: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            if rate != self.sample_rate:
                raise TtsFailed(
                    f"kokoro returned {rate} Hz audio where {self.sample_rate} Hz "
                    "was declared; the frames would play at the wrong pitch"
                )
            frames = _floats_to_pcm16(samples)
            if frames:
                spoke = True
                yield frames
        if not spoke:
            raise TtsFailed(
                "kokoro produced no audio for a non-empty request; refusing to "
                "answer with silence"
            )


# --------------------------------------------------------------------------
# ElevenLabs, hosted
# --------------------------------------------------------------------------

#: The only host this module will ever address. An exact match: a suffix test
#: would wave through ``api.elevenlabs.io.evil.example``.
ALLOWED_HOSTS = frozenset({"api.elevenlabs.io"})


def mask_key(key: str | None) -> str:
    """``<set, N chars>`` or ``<unset>``. Never a prefix, never a tail.

    ``service/integrations.py``'s mask, and its reasoning: a tail is enough to
    confirm a guess, and this string ends up in status bodies and logs.
    """
    if not key:
        return "<unset>"
    return f"<set, {len(key)} chars>"


def ensure_elevenlabs_url(url: str) -> None:
    """Refuse any URL that is not on the allowlist, before it is used."""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.scheme != "https":
        raise TtsFailed("the speech host must be addressed over https")
    if (parts.hostname or "").lower() not in ALLOWED_HOSTS:
        # The host is named; the full URL is not. The API key travels in a
        # header here, but the same rule is kept as billing/googleapps: an
        # error message is not a place to reconstruct a request from.
        raise TtsFailed(f"{parts.hostname!r} is not an allowed speech host")


@dataclass(frozen=True)
class HttpRequest:
    """One outbound call. The allowlist is enforced in ``__post_init__``.

    Checked here rather than inside :func:`urllib_transport` so the guarantee
    holds for *every* transport including the test fakes -- the rule
    ``billing/transport.py`` states and this module copies deliberately.
    """

    url: str
    method: str = "POST"
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | None = None

    def __post_init__(self) -> None:
        ensure_elevenlabs_url(self.url)

    def __repr__(self) -> str:
        """Redacted. This object sits in the frame of every transport failure
        and its headers carry ``xi-api-key``."""
        safe = {
            k: (mask_key(v) if k.lower() == "xi-api-key" else v)
            for k, v in self.headers.items()
        }
        return f"HttpRequest(method={self.method!r}, headers={safe!r})"


@dataclass
class HttpResponse:
    """A streaming response: the status, and frames as they arrive."""

    status: int
    chunks: Iterator[bytes]


class Transport(Protocol):
    def __call__(self, request: HttpRequest) -> HttpResponse: ...


def urllib_transport(request: HttpRequest) -> HttpResponse:
    """The real one. Redirects are refused, because a 3xx would carry the
    ``xi-api-key`` header to wherever it pointed."""

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args: Any, **kwargs: Any) -> None:
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(
        request.url, data=request.body, headers=request.headers, method=request.method
    )
    try:
        handle = opener.open(req, timeout=30)
    except urllib.error.HTTPError as exc:
        # The body may explain the refusal (bad key, unknown voice, quota).
        # Read a bounded prefix of it -- ElevenLabs' errors are small JSON --
        # and pass the explanation on, because "502" alone sends nobody to a
        # fix. The URL is not included.
        detail = ""
        try:
            detail = exc.read(2048).decode("utf-8", "replace").strip()
        except Exception:  # pragma: no cover - best effort
            detail = ""
        raise TtsFailed(
            f"the speech host refused the request: HTTP {exc.code}"
            + (f": {detail}" if detail else "")
        ) from exc
    except urllib.error.URLError as exc:
        raise TtsFailed(
            f"the speech host could not be reached: {exc.reason}"
        ) from exc

    def _chunks() -> Iterator[bytes]:
        with handle:
            while True:
                block = handle.read(4096)
                if not block:
                    return
                yield block

    return HttpResponse(status=handle.status, chunks=_chunks())


def _elevenlabs_key() -> str | None:
    """``ELEVENLABS_API_KEY`` from the environment, or ``~/.kaleo/voice.env``.

    The second source exists because the desktop app's Setup Assistant writes
    provider credentials to ``~/.kaleo/*.env`` and ``service/envfiles.py``
    applies those at start -- but ``voice.env`` is not in that set, and a key
    the user has already provided must not be invisible to the one route that
    needs it. Parsed with the same setdefault semantics: the environment wins.
    """
    key = os.getenv("ELEVENLABS_API_KEY")
    if key:
        return key.strip() or None
    path = Path(
        os.getenv("KALEO_VOICE_ENV") or str(Path.home() / ".kaleo" / "voice.env")
    )
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == "ELEVENLABS_API_KEY":
            return value.strip().strip("'\"") or None
    return None


@dataclass
class ElevenLabsEngine:
    """ElevenLabs Flash v2.5, streamed as raw PCM.

    **Licence: none of ours -- this is a paid hosted API**, opt-in behind a
    key, and it bills per character. It is the upgrade tier, never the
    default, for exactly that reason.

    ``output_format=pcm_24000`` is requested so the response *is* the frame
    contract: no mp3 decode on either side, and the first frames are playable
    the moment they land. The alternative (mp3) cannot be played until enough
    of the file exists to decode, which is the current webview bug.
    """

    transport: Transport = urllib_transport
    name: str = "elevenlabs"
    sample_rate: int = 24_000

    def status(self) -> EngineStatus:
        try:
            key = _elevenlabs_key()
        except Exception as exc:  # pragma: no cover - defensive
            return EngineStatus(
                self.name,
                "unavailable",
                f"could not be inspected: {type(exc).__name__}",
                sample_rate=self.sample_rate,
                streaming=True,
                licence="proprietary hosted API, billed per character",
            )
        if key:
            return EngineStatus(
                self.name,
                "ready",
                f"ELEVENLABS_API_KEY {mask_key(key)}; model {ELEVENLABS_MODEL_ID}",
                sample_rate=self.sample_rate,
                streaming=True,
                licence="proprietary hosted API, billed per character",
            )
        return EngineStatus(
            self.name,
            "unconfigured",
            "no ELEVENLABS_API_KEY in the environment or ~/.kaleo/voice.env",
            hint=(
                "export ELEVENLABS_API_KEY, or write it into ~/.kaleo/voice.env "
                "as ELEVENLABS_API_KEY=..."
            ),
            sample_rate=self.sample_rate,
            streaming=True,
            licence="proprietary hosted API, billed per character",
        )

    def synthesize(self, request: SpeechRequest) -> Iterator[bytes]:
        key = _elevenlabs_key()
        if not key:
            raise TtsUnavailable(
                "elevenlabs cannot speak: no ELEVENLABS_API_KEY in the "
                "environment or ~/.kaleo/voice.env"
            )
        voice = (
            request.voice
            or os.getenv("ELEVENLABS_VOICE_ID")
            or DEFAULT_ELEVENLABS_VOICE
        )
        body = json.dumps(
            {
                "text": request.text,
                "model_id": ELEVENLABS_MODEL_ID,
                "voice_settings": {"speed": request.speed},
            }
        ).encode("utf-8")
        http = HttpRequest(
            url=(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice}/stream"
                f"?output_format=pcm_{self.sample_rate}"
            ),
            method="POST",
            headers={
                "xi-api-key": key,
                "Content-Type": "application/json",
                "Accept": "audio/pcm",
            },
            body=body,
        )
        response = self.transport(http)
        if response.status != 200:
            raise TtsFailed(
                f"the speech host answered HTTP {response.status} rather than "
                "audio"
            )
        spoke = False
        for chunk in response.chunks:
            if chunk:
                spoke = True
                yield chunk
        if not spoke:
            raise TtsFailed(
                "the speech host answered 200 with an empty body; refusing to "
                "report silence as speech"
            )


# --------------------------------------------------------------------------
# macOS `say`, opt-in
# --------------------------------------------------------------------------


@dataclass
class SystemVoiceEngine:
    """macOS ``say``, synthesized to a temp file and then streamed.

    **Licence: Apple's.** Apple engineers have stated on the developer forums
    that system voices "cannot be re-sold/commercialized as your own",
    including cached buffer output. Informal, but real, and the reason this
    engine is a development convenience rather than a shipping answer.

    It is **not streaming**: ``say`` needs a seekable output and refuses a
    pipe (``-o /dev/stdout`` fails with ``-54``), so the whole utterance is
    synthesized before the first byte moves. Measured on an M4: ~1.4-2.3 s for
    one sentence, which is *worse* than the webview's own ``speechSynthesis``.
    Reported as ``streaming: False`` rather than glossed.
    """

    name: str = "system"
    sample_rate: int = 24_000

    def status(self) -> EngineStatus:
        if sys.platform != "darwin":
            return EngineStatus(
                self.name,
                "unavailable",
                f"'say' is a macOS program and this is {sys.platform}",
                sample_rate=self.sample_rate,
                licence="Apple system voices; not licensed for commercial reuse",
            )
        if shutil.which("say") is None:
            return EngineStatus(
                self.name,
                "unavailable",
                "'say' is not on PATH",
                sample_rate=self.sample_rate,
                licence="Apple system voices; not licensed for commercial reuse",
            )
        return EngineStatus(
            self.name,
            "ready",
            "macOS 'say'; opt-in only -- slower to first audio than the "
            "browser's own speechSynthesis, and not streaming",
            sample_rate=self.sample_rate,
            streaming=False,
            licence="Apple system voices; not licensed for commercial reuse",
        )

    def synthesize(self, request: SpeechRequest) -> Iterator[bytes]:
        status = self.status()
        if status.state != "ready":
            raise TtsUnavailable(f"system cannot speak: {status.detail}")
        voice = request.voice or os.getenv("KALEO_SYSTEM_VOICE") or "Samantha"
        with tempfile.TemporaryDirectory(prefix="kaleo-tts-") as tmp:
            out = Path(tmp) / "speech.wav"
            argv = [
                "say",
                "-v",
                voice,
                "-r",
                str(int(175 * request.speed)),
                "-o",
                str(out),
                "--data-format=LEI16@24000",
                "--",
                request.text,
            ]
            try:
                proc = subprocess.run(
                    argv, capture_output=True, timeout=60, check=False
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise TtsFailed(
                    f"system voice failed to run: {type(exc).__name__}: {exc}"
                ) from exc
            if proc.returncode != 0:
                detail = proc.stderr.decode("utf-8", "replace").strip()
                raise TtsFailed(
                    f"system voice {voice!r} failed"
                    + (f": {detail}" if detail else "")
                )
            if not out.is_file() or out.stat().st_size == 0:
                raise TtsFailed(
                    f"system voice {voice!r} wrote no audio; refusing to answer "
                    "with silence"
                )
            # `say` writes a WAV container; the frame contract is bare PCM, so
            # the header is parsed off here rather than shipped inside the
            # payload of another header.
            with wave.open(str(out), "rb") as handle:
                if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
                    raise TtsFailed(
                        "system voice produced "
                        f"{handle.getnchannels()}ch/"
                        f"{handle.getsampwidth() * 8}-bit audio where mono 16-bit "
                        "was asked for"
                    )
                rate = handle.getframerate()
                if rate != self.sample_rate:
                    raise TtsFailed(
                        f"system voice produced {rate} Hz audio where "
                        f"{self.sample_rate} Hz was declared"
                    )
                while True:
                    frames = handle.readframes(2048)
                    if not frames:
                        return
                    yield frames


# --------------------------------------------------------------------------
# Selection and the route's two entry points
# --------------------------------------------------------------------------


def build_engines(*, transport: Transport | None = None) -> dict[str, TtsEngine]:
    """Every engine this build knows about, keyed by name.

    Construction is free -- no model is loaded, no call is made -- so this is
    safe to call per request and there is no client to reuse. (The Firestore
    client-per-request problem in known issue 5 is a different shape: that one
    opens a connection.)
    """
    return {
        "kokoro": KokoroEngine(),
        "elevenlabs": (
            ElevenLabsEngine(transport=transport)
            if transport is not None
            else ElevenLabsEngine()
        ),
        "system": SystemVoiceEngine(),
    }


def _configured_order() -> tuple[str, ...]:
    raw = os.getenv("KALEO_TTS_ENGINE", "").strip()
    if not raw:
        return DEFAULT_ENGINE_ORDER
    # A comma-separated list is accepted so an operator can express a
    # preference ladder, not just a single pin.
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def resolve_engine(
    engines: dict[str, TtsEngine], request: SpeechRequest
) -> TtsEngine:
    """The engine that will speak this request, or a :class:`TtsError`.

    Precedence: the ``engine`` field, then ``KALEO_TTS_ENGINE``, then
    :data:`DEFAULT_ENGINE_ORDER`. A named engine that is not ready is a
    **refusal naming why**, never a silent downgrade to a different voice --
    the caller asked for a specific one and is entitled to know it did not
    happen.
    """
    if request.engine is not None:
        engine = engines.get(request.engine)
        if engine is None:
            raise ValueError(
                f"unknown engine {request.engine!r}; known engines are "
                f"{', '.join(sorted(engines))}"
            )
        status = engine.status()
        if status.state != "ready":
            raise TtsUnavailable(
                f"engine {request.engine!r} was asked for but is "
                f"{status.state}: {status.detail}"
                + (f". {status.hint}" if status.hint else "")
            )
        return engine

    reasons: list[str] = []
    for name in _configured_order():
        engine = engines.get(name)
        if engine is None:
            reasons.append(f"{name}: not an engine in this build")
            continue
        status = engine.status()
        if status.state == "ready":
            return engine
        reasons.append(f"{name}: {status.detail}")
    raise TtsUnavailable(
        "no speech engine is configured, so the service cannot speak. "
        + "; ".join(reasons)
        + ". Until one is configured the client should use its own "
        "speechSynthesis and say out loud that it is doing so."
    )


def speak_report(engines: dict[str, TtsEngine]) -> dict[str, Any]:
    """One read-only view of the voice stack, for ``GET /speak``.

    Always answers. A probe that raises becomes that engine's own state, the
    ``service/integrations.py`` rule, so one broken engine cannot hide the
    others.
    """
    order = _configured_order()
    reports = []
    for name, engine in engines.items():
        try:
            reports.append(engine.status().as_dict())
        except Exception as exc:  # pragma: no cover - defensive
            reports.append(
                EngineStatus(
                    name, "unavailable", f"probe raised {type(exc).__name__}"
                ).as_dict()
            )
    selected = None
    for name in order:
        engine = engines.get(name)
        if engine is not None:
            try:
                if engine.status().state == "ready":
                    selected = name
                    break
            except Exception:  # pragma: no cover - defensive
                continue
    return {
        "engines": reports,
        "order": list(order),
        "selected": selected,
        "formats": list(AUDIO_FORMATS),
        "max_text_chars": MAX_TEXT_CHARS,
        # Stated rather than implied: with nothing configured the caller keeps
        # the voice it has today, and is told that is what happened.
        "fallback": (
            None
            if selected
            else "client speechSynthesis -- no service engine is configured"
        ),
    }


def synthesize(
    engines: dict[str, TtsEngine], request: SpeechRequest
) -> tuple[TtsEngine, Iterator[bytes]]:
    """Pick an engine and start it.

    Returns the engine as well as the stream so the route can report *which*
    voice answered in a response header before any audio is written -- a
    caller must be able to tell a hosted voice from a local one without
    listening to it.

    The first frame is pulled here, inside the caller's ``try``, so an engine
    that fails immediately still produces an HTTP error rather than a 200 with
    a truncated body: once bytes are on the wire this server (HTTP/1.0,
    close-delimited) has no way left to signal a failure.
    """
    engine = resolve_engine(engines, request)
    stream = engine.synthesize(request)
    try:
        first = next(stream)
    except StopIteration:
        raise TtsFailed(
            f"engine {engine.name!r} produced no audio at all for a non-empty "
            "request"
        ) from None

    def _framed() -> Iterator[bytes]:
        yield first
        yield from stream

    return engine, _framed()

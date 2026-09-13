"""Cloud Run service: prompt in, KiCad board out.

A single HTTP surface over :func:`silkscreen.agents.generate_pcb`, built on the
standard library so the container stays small and the dependency list stays
honest. Datasheet facts persist to Firestore, so the second request for a part
skips the most expensive stage in the pipeline.

Run locally::

    python -m service.app

Deploy::

    gcloud run deploy silkscreen --source . --region us-central1
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import math
import os
import sys
import time
import traceback
import urllib.parse
import uuid
from collections.abc import Callable
from dataclasses import fields, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from service import billing_routes as _billing

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

from silkscreen.agents import ModelError, generate_pcb  # noqa: E402
from silkscreen.agents.datasheet import PartFacts  # noqa: E402
from silkscreen.agents.desk import (  # noqa: E402
    DeskCandidate,
    DeskValidationError,
    resolve_desk,
)
from silkscreen.agents.effort import UNSET, profile_for  # noqa: E402
from silkscreen.agents.grounding import (  # noqa: E402
    BatchingEmbedder,
    GroundingError,
    build_index,
    ground_findings,
    load_pages,
    pages_for_part,
    store_pages,
)
from silkscreen.agents.model import GeminiModel  # noqa: E402
from silkscreen.agents.resilience import (  # noqa: E402
    AllProvidersFailed,
    FallbackModel,
    Provider,
)
from silkscreen.agents.retrieval import GeminiEmbedder  # noqa: E402
from silkscreen.agents.transcribe import transcribe_audio  # noqa: E402
from silkscreen.board import emit_kicad_pcb  # noqa: E402
from silkscreen.constraints import (  # noqa: E402
    parse_constraint_manifest,
    verify_constraint_manifest,
)
from silkscreen.enclosure import rules as enclosure_rules  # noqa: E402
from silkscreen.fab import fab_files  # noqa: E402
from silkscreen.order import (  # noqa: E402
    OrderOptions,
    SolderMaskColour,
    SurfaceFinish,
    order_manifest,
    preflight,
)
from silkscreen.placement.agent import PlacementPolicyError  # noqa: E402
from silkscreen.placement.api import repair_request, run_to_dict  # noqa: E402
from silkscreen.placement.traces import (  # noqa: E402
    FactFailureTraceStore,
    FailureTraceStore,
    JsonlFailureTraceStore,
    build_failure_traces,
)
from silkscreen.units import to_mm  # noqa: E402

from . import amend as _amend  # noqa: E402
from . import deliver as _deliver  # noqa: E402
from . import envfiles as _envfiles  # noqa: E402
from . import inbox as _inbox  # noqa: E402
from . import integrations as _integrations  # noqa: E402
from . import metering as _metering  # noqa: E402
from . import runs as _runs  # noqa: E402
from . import setup as _setup  # noqa: E402
from . import steps as _steps  # noqa: E402
from . import tts as _tts  # noqa: E402
from .cache import FactStore, MemoryFactStore  # noqa: E402
from .configuration import configuration_status  # noqa: E402
from .models import (  # noqa: E402
    model_catalog,
    select_model,
    select_quota_rpm,
    select_thinking_level,
)
from .quota import GEMINI_REQUEST_PACER, RequestPacer  # noqa: E402

__all__ = [
    "Handler",
    "build_embedder",
    "build_model",
    "build_ollama_model",
    "build_pages_store",
    "build_failure_trace_store",
    "build_store",
    "build_tinker_model",
    "placement_policy_status",
    "resolve_placement_policy",
    "caused_by_model_failure",
    "generate",
    "make_server",
    "page_cache_key",
    "transcribe_request",
    "desk_request",
]

MAX_BODY_BYTES = 1 << 20
#: A consent form is three short fields; 4 KiB is already generous.
MAX_FORM_BYTES = 4096
#: A retina desktop PNG plus base64 overflows the shared 1 MiB cap. Desk
#: resolve is the only route that needs this; every other POST stays at 1 MiB.
DESK_MAX_BODY_BYTES = 12 << 20
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

#: Audio types /transcribe accepts. The first six are the formats Gemini's
#: audio documentation names outright (plus their common MIME aliases --
#: browsers say audio/mpeg for mp3 and audio/x-wav for wav). webm, mp4 and
#: x-m4a are what this endpoint's real clients actually record: Chromium's
#: MediaRecorder produces webm/opus and WKWebView produces mp4/aac, and Gemini
#: accepts both -- rejecting them here would refuse the desktop app's own
#: microphone. Compared case-insensitively with any ";codecs=..." suffix
#: stripped, since MediaRecorder reports "audio/webm;codecs=opus".
TRANSCRIBE_AUDIO_TYPES = frozenset(
    {
        "audio/wav",
        "audio/x-wav",
        "audio/mp3",
        "audio/mpeg",
        "audio/aiff",
        "audio/x-aiff",
        "audio/aac",
        "audio/ogg",
        "audio/flac",
        "audio/webm",
        "audio/mp4",
        "audio/x-m4a",
    }
)
#: Peak sample deviation (0-128, the browser's byte time-domain data centred on
#: 128) that counts as speech, mirroring ``LOUDNESS_THRESHOLD`` in
#: app/src/lib/wake-word.ts. A client that measured a quieter window than this
#: recorded no speech, and transcribing it is a paid call whose likeliest
#: answer is the wake name the prompt primes -- four seconds of near-silence
#: was measured coming back as "Ada". The field is optional: a caller that
#: cannot measure loudness sends none and keeps the previous behaviour.
TRANSCRIBE_PEAK_THRESHOLD = 8.0
TRANSCRIBE_PEAK_MAX = 128.0

PAGES_COLLECTION = "datasheet_pages"
MAX_GROUND_PARTS = 25
MAX_ENCLOSURE_STYLE_CHARS = 500


def page_cache_key(part: str, url: str) -> str:
    return f"{part}\x00{hashlib.sha256(url.encode('utf-8')).hexdigest()[:16]}"


#: The environment variable holding the application bearer token. Setting it
#: turns the gate on; leaving it unset leaves the service open, which is what
#: local use wants (``python -m service.app`` behind a loopback socket, the
#: desktop app and the SPA both same-origin) and what a deploy must never do.
#: ``scripts/deploy.sh`` wires it from Secret Manager unless ``--no-token``.
ACCESS_TOKEN_ENV = "SILKSCREEN_ACCESS_TOKEN"

#: Routes the gate never covers, whatever the token says.
#:
#: ``/healthz`` and ``/readyz`` are Cloud Run's start-up and liveness probes,
#: which carry no header of ours; gating them would fail every revision.
#: ``/billing/webhook`` is Stripe's, and Stripe cannot send our token -- that
#: request authenticates itself with an HMAC over its own raw body, which is a
#: stronger check than a shared bearer, and gating it would silently drop every
#: payment event.
#:
#: ``GET`` of the built bundle is exempt too, but by shape rather than by name
#: (see :meth:`Handler._authorized`): a browser navigating to ``/`` cannot put
#: a bearer header on a document request, so gating the bundle would serve a
#: 401 page instead of the app. The bundle is public build output with no
#: secret in it; every route it then calls is gated, so the token still gates
#: everything that spends money.
AUTH_EXEMPT_ROUTES = frozenset({"/healthz", "/readyz", "/billing/webhook"})


def access_token() -> str:
    """The configured bearer token, or ``""`` when there is no gate.

    Read per request rather than at import: the process is started by
    ``deploy.sh``-set environment in production and by a fixture in tests, and
    a value frozen at import time cannot be either.
    """
    return (os.environ.get(ACCESS_TOKEN_ENV) or "").strip()


#: Where the built web bundle lives. The override exists because the container
#: copies the bundle to a path the repo layout does not imply.
WEB_DIST = Path(
    os.getenv("SILKSCREEN_WEB_DIST")
    or Path(__file__).resolve().parent.parent / "frontend" / "dist"
)

# Spelled out rather than taken from mimetypes: on Windows mimetypes reads the
# registry, which routinely maps .js to text/plain, and a browser hard-refuses
# a module script served with the wrong type. That failure would appear on a
# developer's machine and disappear in the container.
_CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".ico": "image/x-icon",
    ".jpg": "image/jpeg",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".map": "application/json",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ttf": "font/ttf",
    ".txt": "text/plain; charset=utf-8",
    ".webp": "image/webp",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}
_DEFAULT_CONTENT_TYPE = "application/octet-stream"


class _ResponseSerializationError(RuntimeError):
    """A response or stream frame could not be represented as strict JSON."""


class RequestError(ValueError):
    """A caller-fixable field-validation failure, and only that.

    Subclassing ``ValueError`` keeps every existing ``raise ValueError(...)``
    call site source-compatible with `isinstance(exc, ValueError)` checks
    elsewhere in the codebase (engine code legitimately raises plain
    ``ValueError`` for internal invariants -- see CLAUDE.md's `edge_refs`
    convention). ``_error_response`` deliberately checks
    ``isinstance(exc, RequestError)``, never a bare ``ValueError``, so a
    pipeline's own ``ValueError`` (board generation, packing, ...) falls
    through to the sanitized 500 path instead of leaking its raw text as a
    400. Raise this only at true request-shape validation sites in this
    module -- a missing/malformed JSON field, an out-of-range value, an
    unknown option -- never from engine/pipeline code.
    """


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"non-finite number {value} is not valid JSON")


def _parse_json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        _reject_nonfinite_json(value)
    return number


def _json_line(payload: dict[str, Any]) -> bytes:
    """Encode one NDJSON frame without emitting JavaScript-only NaN values."""
    try:
        return (json.dumps(payload, allow_nan=False) + "\n").encode()
    except (TypeError, ValueError) as exc:
        raise _ResponseSerializationError(
            "stream frame is not JSON serializable"
        ) from exc


def _refs_by_spec_name(spec, board) -> dict[str, str]:
    """Spec part names to the reference designators the board gave them.

    A finding names parts the way the *spec* does -- ``AMS1117-3.3``,
    ``c_bulk_vin`` -- because review_circuit validates them against the spec.
    The board names the same parts ``U1`` and ``C1``. Without this map a client
    that highlights by ref matches nothing at all, which looks exactly like a
    finding about no part rather than a lookup failure.

    ``build_board`` assigns refs by walking devices then passives in spec
    order, so the two lists pair up positionally. The device half is checked
    against the value it carries; if anything about that pairing stops holding,
    an empty map is the honest answer, because a wrong ref highlights the
    wrong part.
    """
    names = [d.name for d in spec.devices] + [p.name for p in spec.passives]
    parts = list(board.parts)
    if len(names) != len(parts):
        return {}
    for device, part in zip(spec.devices, parts, strict=False):
        if part.value != device.name:
            return {}
    return {name: part.ref for name, part in zip(names, parts, strict=True)}


def _schematic_dict(spec, refs: dict[str, str]) -> dict[str, Any]:
    """The validated circuit topology in a renderer-sized wire format.

    ``CircuitSpec`` is deliberately an engine object, not an HTTP contract.
    Sending its dataclass fields directly would make the browser understand
    enums, tuples and the endpoint mini-language.  Resolve those here instead:
    every part has one stable spec id and its board reference, and every net
    endpoint names both the logical pin and its physical number.

    Geometry is absent on purpose.  The service owns electrical truth; a view
    owns layout.  Versioning the block lets a later renderer add richer symbol
    metadata without guessing which shape it received.
    """
    devices = {device.name: device for device in spec.devices}
    passives = {passive.name: passive for passive in spec.passives}

    parts: list[dict[str, Any]] = []
    for device in spec.devices:
        parts.append(
            {
                "id": device.name,
                "ref": refs.get(device.name),
                "kind": "device",
                "value": device.name,
                "symbol": device.symbol,
                "pins": [
                    {"name": name, "number": number}
                    for name, number in device.pins.items()
                ],
            }
        )
    for passive in spec.passives:
        parts.append(
            {
                "id": passive.name,
                "ref": refs.get(passive.name),
                "kind": passive.type.value,
                "value": passive.value,
                "symbol": None,
                "pins": [
                    {"name": "1", "number": "1"},
                    {"name": "2", "number": "2"},
                ],
            }
        )

    nets: list[dict[str, Any]] = []
    for connection in spec.connections:
        endpoints: list[dict[str, Any]] = []
        for raw in connection.endpoints:
            part_id, _, pin = raw.rpartition(".")
            device = devices.get(part_id)
            number = device.pins.get(pin) if device is not None else pin
            # The spec was validated before this point, so the only other
            # legitimate endpoint owner is a declared two-terminal passive.
            if device is None and part_id not in passives:
                continue
            endpoints.append(
                {
                    "part_id": part_id,
                    "ref": refs.get(part_id),
                    "pin": pin,
                    "number": number,
                }
            )
        nets.append({"name": connection.net, "endpoints": endpoints})

    return {"version": 1, "parts": parts, "nets": nets}


def _finding_dict(finding, refs: dict[str, str] | None = None) -> dict[str, Any]:
    """One review finding, whole.

    ``blockers`` flattens a finding to a single string, dropping the severity,
    the detail, the citation and the suggested fix -- everything a reader needs
    in order to act on it.

    ``parts`` keeps the spec's names, which is the vocabulary the title and
    detail are written in; ``refs`` carries the same parts as they are labelled
    on the board, so a reader can point at them without guessing.

    ``origin`` is the audit package's distinction reaching the wire: ``proven``
    is a rule that measured the board and carries the measurement in
    ``evidence``, ``suggested`` is a model's proposal. Everything on these two
    routes comes from :mod:`silkscreen.agents.review`, an adversarial critic
    whose findings are model proposals and carry no rule and no measurement --
    so they serialise as ``suggested`` with ``rule`` and ``evidence`` null,
    which is a statement about them and not a placeholder. A finding that does
    carry the audit fields (``silkscreen.audit``) serialises its own, so the
    label can never claim a measurement nothing made.
    """
    refs = refs or {}
    origin = getattr(finding, "origin", None)
    evidence = getattr(finding, "evidence", None)
    rule = getattr(finding, "rule", None)
    return {
        "severity": finding.severity.value,
        "title": finding.title,
        "detail": finding.detail,
        "parts": list(finding.parts),
        "refs": [refs[p] for p in finding.parts if p in refs],
        "citation": finding.citation,
        "suggested_fix": finding.suggested_fix,
        "origin": getattr(origin, "value", origin) or "suggested",
        "rule": rule or None,
        "evidence": evidence or None,
    }


def _rect_mm(x_nm: int, y_nm: int, w_nm: int, h_nm: int) -> list[float]:
    """A rectangle as ``[x, y, w, h]`` in millimetres, min-corner first."""
    return [
        round(to_mm(x_nm), 3),
        round(to_mm(y_nm), 3),
        round(to_mm(w_nm), 3),
        round(to_mm(h_nm), 3),
    ]


_ORDER_ENUMS = {
    "surface_finish": SurfaceFinish,
    "mask_colour": SolderMaskColour,
}


def order_options(raw: Any) -> OrderOptions:
    """Build :class:`OrderOptions` from a request body, or refuse it.

    Unknown keys are rejected rather than ignored: a client that misspells
    ``quantity`` is asking for a different order than the one it would get,
    and silently shipping five boards instead of fifty is the expensive
    failure mode here.
    """
    if not isinstance(raw, dict):
        raise RequestError("'order' must be an object of order options")
    known = {f.name for f in fields(OrderOptions)}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise RequestError(
            f"unknown order option(s): {', '.join(unknown)}; "
            f"supported: {', '.join(sorted(known))}"
        )
    kwargs = dict(raw)
    for field_name, enum in _ORDER_ENUMS.items():
        if field_name in kwargs:
            try:
                kwargs[field_name] = enum(kwargs[field_name])
            except ValueError:
                allowed = ", ".join(m.value for m in enum)
                raise RequestError(
                    f"{field_name} must be one of {allowed}, "
                    f"got {kwargs[field_name]!r}"
                ) from None
    try:
        return OrderOptions(**kwargs)
    except ValueError as exc:
        # OrderOptions.__post_init__ (engine/silkscreen/order.py) validates
        # values against fab capabilities (layer counts, stock thicknesses,
        # …) and raises plain ValueError naming the field -- caller input,
        # not an internal invariant, so it belongs in the 400 taxonomy here
        # rather than falling through as an unrelated internal failure.
        raise RequestError(str(exc)) from exc


def _order_block(board, spec, options: OrderOptions) -> dict[str, Any]:
    """Preflight, manifest and fab files for one board.

    The files are small enough to inline -- a dozen text artefacts totalling a
    few kilobytes -- so the client gets everything it needs to show, zip or
    download an order in the same response that produced the board.
    """
    pre = preflight(board, spec=spec, options=options)
    return {
        "manifest": order_manifest(board, options, pre),
        "issues": [issue.as_dict() for issue in pre.issues],
        "orderable": pre.orderable,
        "files": [
            {"filename": layer.filename, "content": layer.content}
            for layer in fab_files(board)
        ],
    }


def _placements_dict(board) -> dict[str, Any]:
    """Every rectangle a renderer needs, with rotation already applied.

    ``parts`` in the response names refs and footprints only, which is enough
    to list a board and not enough to draw one. This carries the geometry: the
    courtyard each part occupies and every pad inside it, absolute, in the
    solver's Y-up frame.

    Rotation is resolved here rather than on the wire. A rotated part's box and
    pads arrive already transformed, so a client's only coordinate work is the
    single Y flip its own frame needs -- the repository's rule that exactly one
    place owns each frame change, applied across the HTTP boundary.
    """
    parts: list[dict[str, Any]] = []
    for part in board.parts:
        fp = part.footprint
        cw, ch = fp.courtyard_w_nm, fp.courtyard_h_nm
        # x_nm/y_nm are the courtyard's bottom-left corner, Y up; the
        # footprint's own pad coordinates are centred on its anchor. A
        # 90-degree rotation is about the box, so its extents swap.
        box_w, box_h = (2 * ch, 2 * cw) if part.rotated else (2 * cw, 2 * ch)

        pads: list[dict[str, Any]] = []
        for pad in fp.pads:
            # Offsets from that same bottom-left corner. A footprint's own pad
            # coordinates are KiCad's, so Y counts downward from the anchor --
            # the same flip kicad.py performs on the read side, and the one
            # emit_kicad_pcb relies on when it writes these pads out verbatim.
            # Skipping it mirrors every multi-row package inside its own
            # courtyard, silently disagreeing with the board file we serve
            # alongside. Rotation then maps (ox, oy) to (height - oy, ox),
            # exactly as the solver models it.
            ox, oy = pad.x_nm + cw, ch - pad.y_nm
            if part.rotated:
                ox, oy = 2 * ch - oy, ox
                pw, ph = pad.h_nm, pad.w_nm
            else:
                pw, ph = pad.w_nm, pad.h_nm
            pads.append(
                {
                    "number": pad.number,
                    "net": pad.net or None,
                    "rect_mm": _rect_mm(
                        part.x_nm + ox - pw // 2,
                        part.y_nm + oy - ph // 2,
                        pw,
                        ph,
                    ),
                }
            )

        parts.append(
            {
                "ref": part.ref,
                "footprint": fp.name,
                "value": part.value or None,
                "layer": str(part.layer),
                "rotated": bool(part.rotated),
                "x_mm": round(to_mm(part.x_nm), 3),
                "y_mm": round(to_mm(part.y_nm), 3),
                "courtyard_mm": _rect_mm(part.x_nm, part.y_nm, box_w, box_h),
                "pads": pads,
            }
        )

    return {
        "board_mm": [
            round(to_mm(board.width_nm), 3),
            round(to_mm(board.height_nm), 3),
        ],
        "frame": "solver-y-up",
        "parts": parts,
    }


def transcribe_request(
    payload: dict[str, Any],
) -> tuple[bytes, str, str | None, float | None]:
    """Decode and validate one /transcribe body, or refuse it as a ValueError.

    Every message names its field: a 400 that just says "bad request" costs a
    round of client-side guessing. The overall size is already bounded by
    ``MAX_BODY_BYTES`` before this runs, so the only checks left are shape.
    """
    raw = payload.get("audio_b64")
    if not isinstance(raw, str) or not raw.strip():
        raise RequestError("'audio_b64' is required")
    try:
        # validate=True: silently discarding non-alphabet bytes would decode
        # a corrupted upload into different audio rather than refusing it.
        audio = base64.b64decode(raw, validate=True)
    except ValueError:  # binascii.Error is a ValueError
        raise RequestError("'audio_b64' is not valid base64") from None
    # No zero-byte check: valid base64 of nothing is "", which the required
    # check above already refused.

    mime = payload.get("mime_type")
    if not isinstance(mime, str) or not mime.strip():
        raise RequestError("'mime_type' is required")
    # MediaRecorder reports "audio/webm;codecs=opus"; the parameter is real
    # information but the allow-list is about the container.
    base_type = mime.split(";", 1)[0].strip().lower()
    if base_type not in TRANSCRIBE_AUDIO_TYPES:
        raise RequestError(
            "'mime_type' must be one of "
            f"{', '.join(sorted(TRANSCRIBE_AUDIO_TYPES))}; got {mime!r}"
        )

    language = payload.get("language")
    if language is not None and not isinstance(language, str):
        raise RequestError("'language' must be a string")
    language = (language or "").strip() or None

    raw_peak = payload.get("peak")
    peak: float | None = None
    if raw_peak is not None:
        if isinstance(raw_peak, bool) or not isinstance(raw_peak, (int, float)):
            raise RequestError("'peak' must be a number")
        if not math.isfinite(raw_peak):
            raise RequestError("'peak' must be a finite number")
        if not 0.0 <= float(raw_peak) <= TRANSCRIBE_PEAK_MAX:
            raise RequestError(
                f"'peak' must be between 0 and {TRANSCRIBE_PEAK_MAX:g}; "
                f"got {raw_peak!r}"
            )
        peak = float(raw_peak)
    return audio, base_type, language, peak


def _desk_number(payload: dict[str, Any], name: str) -> float:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RequestError(f"'{name}' must be a number")
    if not math.isfinite(value):
        raise RequestError(f"'{name}' must be a finite number")
    return float(value)


def _desk_candidates(raw: object) -> tuple[DeskCandidate, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise RequestError("'candidates' must be a list of objects")
    out: list[DeskCandidate] = []
    for index, item in enumerate(raw):
        where = f"candidates[{index}]"
        if not isinstance(item, dict):
            raise RequestError(f"{where} must be an object")
        testid = item.get("testid")
        if not isinstance(testid, str) or not testid.strip():
            raise RequestError(f"{where} needs a 'testid' string")
        attrs_raw = item.get("attrs")
        attrs: dict[str, str] = {}
        if attrs_raw is not None:
            if not isinstance(attrs_raw, dict):
                raise RequestError(f"{where}.attrs must be an object")
            for key, value in attrs_raw.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    raise RequestError(
                        f"{where}.attrs keys and values must be strings"
                    )
                attrs[key] = value
        tab = item.get("tab")
        if tab is not None and not isinstance(tab, str):
            raise RequestError(f"{where}.tab must be a string")
        tab = (tab or "").strip() or None
        out.append(DeskCandidate(testid=testid.strip(), attrs=attrs, tab=tab))
    return tuple(out)


def desk_request(
    payload: dict[str, Any],
) -> tuple[bytes, str, float, float, int | None, int | None, tuple[DeskCandidate, ...]]:
    """Decode and validate one /desk/resolve body, or refuse it as a ValueError.

    Every message names its field. The overall size is already bounded by
    ``DESK_MAX_BODY_BYTES`` before this runs.
    """
    raw = payload.get("png_b64")
    if not isinstance(raw, str) or not raw.strip():
        raise RequestError("'png_b64' is required")
    try:
        png = base64.b64decode(raw, validate=True)
    except ValueError:
        raise RequestError("'png_b64' is not valid base64") from None
    if not png.startswith(_PNG_MAGIC):
        raise RequestError("'png_b64' is not a PNG")

    utterance = payload.get("utterance")
    if not isinstance(utterance, str) or not utterance.strip():
        raise RequestError("'utterance' is required")

    cursor_x = _desk_number(payload, "cursor_x")
    cursor_y = _desk_number(payload, "cursor_y")

    width = payload.get("width")
    height = payload.get("height")
    if width is not None:
        if isinstance(width, bool) or not isinstance(width, (int, float)):
            raise RequestError("'width' must be a positive integer")
        width = int(width)
        if width <= 0:
            raise RequestError("'width' must be a positive integer")
    if height is not None:
        if isinstance(height, bool) or not isinstance(height, (int, float)):
            raise RequestError("'height' must be a positive integer")
        height = int(height)
        if height <= 0:
            raise RequestError("'height' must be a positive integer")

    candidates = _desk_candidates(payload.get("candidates"))
    return (
        png,
        utterance.strip(),
        cursor_x,
        cursor_y,
        width,
        height,
        candidates,
    )


def _enclosure_dict(
    enclosure, *, include_brief: bool = False
) -> dict[str, Any] | None:
    """The additive ``enclosure`` response block (docs/ai-cad-plan.md).

    ``None`` is the contract's honest degradation: the stage failed or was
    skipped -- including because the ``cad`` extra is not installed, which is
    refused in words rather than degraded to a lesser case -- and the board
    is still the product.

    On success the STEP assembly rides the JSON exactly as ``kicad_pcb``
    does, under ``step``: ISO 10303-21 is ASCII and self-contained, which is
    what lets the one-shot route ship a whole case without writing a file.
    The v1 ``scad``/``params``/``fit``/``engine`` keys were removed on
    2026-09-08 with the OpenSCAD emitter (docs/ai-cad-plan.md v3); there is
    one engine, and ``kernel`` -- the B-rep verifier's clause-by-clause
    receipt, each clause with its signed margin in mm -- is the only receipt.
    ``files`` holds the paths the stage wrote as strings, null/empty on the
    one-shot route, which writes nothing durable. Additive inside
    ``kernel``: ``thin_band_mm`` is ``rules.THIN_MARGIN_NM`` in mm, the band
    within which a *passing* clause is only nominally passing. That number is
    mechanical, so the engine states it rather than a client inventing one; a
    client that does not see the key simply has no band. ``brief`` (the text
    the model was shown) is emitted only when ``include_brief`` is set, i.e.
    on the steps route: the one-shot response must never grow raw prompt or
    model text.
    """
    if enclosure is None:
        return None
    kernel = getattr(enclosure, "kernel", None)
    exports = getattr(enclosure, "exports", None)
    block: dict[str, Any] = {
        "step": enclosure.step_text,
        "warnings": [] if kernel is None else list(kernel.warnings),
        "repair_rounds": enclosure.repair_rounds,
        "kernel": (
            None
            if kernel is None
            else {
                "passed": bool(kernel.passed),
                "clauses": [
                    {
                        "name": c.name,
                        "passed": bool(c.passed),
                        "margin_mm": round(to_mm(c.margin_nm), 3),
                        "detail": c.detail,
                    }
                    for c in kernel.clauses
                ],
                "warnings": list(kernel.warnings),
                "thin_band_mm": round(to_mm(enclosure_rules.THIN_MARGIN_NM), 3),
            }
        ),
        "files": {
            "step": None if exports is None else str(exports.step),
            "base_stl": None if exports is None else str(exports.base_stl),
            "lid_stl": None if exports is None else str(exports.lid_stl),
            "snapshots": [str(p) for p in getattr(enclosure, "snapshots", ())],
        },
    }
    if include_brief:
        block["brief"] = getattr(enclosure, "brief", "") or ""
    return block


def caused_by_model_failure(exc: BaseException) -> bool:
    """True if a model outage is anywhere under this exception.

    The pipeline wraps a failed call in ProposalError, which is a RuntimeError
    and not a ModelError -- so a Gemini outage and a Gemini response we could
    not use arrive as the same type. They are different failures: one is ours
    to retry, the other is the caller's prompt to fix. Walking the cause chain
    is what keeps a 503 upstream from being reported as our 500.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (ModelError, AllProvidersFailed)):
            return True
        current = current.__cause__ or current.__context__
    return False


def _error_response(exc: BaseException) -> tuple[int, dict[str, Any]]:
    """One failed run, as the status and body a caller should be told about.

    Both POST routes answer failures the same way and differ only in framing:
    the one-shot route sends this as the whole response, and the streaming
    route wraps it in a final ``run.error`` frame. Holding the taxonomy in one
    place is also what makes it fixable in one place: only ``RequestError``
    (raised solely at field-validation sites in this module) answers as a 400
    with its raw message. A plain ``ValueError`` -- including one raised deep
    in the pipeline, e.g. by board generation or packing -- is an internal
    failure signal, not a caller mistake, and falls through to the 500 branch
    below with a generated id and a logged traceback rather than leaking its
    text.
    """
    if isinstance(exc, RequestError):
        return 400, {"error": str(exc)}
    if isinstance(exc, _metering.InsufficientCredit):
        # 402 Payment Required, and it is the one refusal that costs nothing:
        # `Metering.begin` reserves before the first model call, so a run
        # turned away here has spent nothing. A run already under way is never
        # stopped for credit -- see billing/ledger.py::OveragePolicy.
        return 402, {"error": str(exc), "reason": "insufficient_credit"}
    if isinstance(exc, _runs.RunCancelled):
        # The caller asked for this, so it is neither a 500 nor a 502. 409:
        # the run's state and the request now conflict. The message names the
        # route that did it so a cancel nobody remembers pressing is traceable.
        return 409, {"error": str(exc), "reason": "cancelled"}
    if isinstance(
        exc,
        (
            AllProvidersFailed,
            GroundingError,
            ModelError,
            PlacementPolicyError,
            DeskValidationError,
        ),
    ):
        # Upstream is down, not the caller's fault: 502, not 500.
        return 502, {"error": str(exc)}
    if caused_by_model_failure(exc):
        return 502, {"error": str(exc)}
    # The traceback goes to the log, not to the caller. This is a public
    # endpoint, and a stack trace hands an anonymous client our file layout
    # and internal call structure. The id is what makes the two halves
    # joinable when someone reports a failure.
    error_id = uuid.uuid4().hex[:12]
    trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    sys.stderr.write(f"error {error_id}: {type(exc).__name__}: {exc}\n{trace}\n")
    return 500, {"error": "internal error", "error_id": error_id}


def _speak_error(exc: BaseException) -> tuple[int, dict[str, Any]]:
    """One failed ``/speak``, as a status and a body that names the fix.

    Kept beside ``_error_response`` rather than folded into it because the
    two speech failures are neither caller mistakes nor upstream model
    failures, and both carry a message written to be read by a person:

    * ``TtsUnavailable`` -> **503**. Nothing is configured, or the engine that
      was named is not ready. The client's correct response is to fall back to
      its own ``speechSynthesis`` and say so, which is why the message names
      what would make the service speak instead.
    * ``TtsFailed`` -> **502**. A configured engine was asked and could not
      deliver -- the hosted host refused, a model failed to load. Upstream is
      down, not the caller's fault, the same call ``_error_response`` makes
      for ``ModelError``.

    Anything else falls through to the shared taxonomy, so an unexpected
    exception still gets a sanitized 500 with a logged traceback and an id.
    """
    if isinstance(exc, _tts.TtsUnavailable):
        return 503, {"error": str(exc), "fallback": "client speechSynthesis"}
    if isinstance(exc, _tts.TtsFailed):
        return 502, {"error": str(exc)}
    return _error_response(exc)


def build_store() -> FactStore:
    """Firestore when deployed, in-memory when not configured."""
    if os.getenv("GOOGLE_CLOUD_PROJECT") and os.getenv("USE_FIRESTORE", "1") != "0":
        from .cache import FirestoreFactStore

        return FirestoreFactStore()
    return MemoryFactStore()


def build_pages_store() -> FactStore:
    if os.getenv("GOOGLE_CLOUD_PROJECT") and os.getenv("USE_FIRESTORE", "1") != "0":
        from .cache import FirestoreFactStore

        return FirestoreFactStore(PAGES_COLLECTION)
    return MemoryFactStore()


def build_failure_trace_store() -> FailureTraceStore:
    """Firestore in Cloud Run, append-only JSONL for local post-training."""
    if os.getenv("GOOGLE_CLOUD_PROJECT") and os.getenv("USE_FIRESTORE", "1") != "0":
        from .cache import FirestoreFactStore

        return FactFailureTraceStore(
            FirestoreFactStore("placement_failure_traces")
        )
    configured = os.getenv("PLACEMENT_FAILURE_TRACE_PATH", "").strip()
    path = (
        Path(configured)
        if configured
        else Path(__file__).resolve().parent.parent
        / "artifacts"
        / "placement-failure-traces.jsonl"
    )
    return JsonlFailureTraceStore(path)


def build_embedder() -> BatchingEmbedder:
    return BatchingEmbedder(GeminiEmbedder())


def build_model():
    """Primary Gemini model with a cheaper tier and open-weights Gemma behind it.

    Three tiers, not one: a rate limit or a transient 5xx on the primary should
    degrade the answer, not lose the request. Gemma is a different model family
    behind the same API, so an outage or quota exhaustion shared by both Gemini
    tiers still leaves one rung standing. One attempt only on that last rung:
    by then the caller has already waited through four Gemini attempts.
    """
    from silkscreen.agents.model import CHEAP_MODEL, DEFAULT_MODEL, GEMMA_MODEL

    return FallbackModel(
        providers=[
            Provider("gemini-primary", GeminiModel(DEFAULT_MODEL), attempts=2),
            Provider("gemini-cheap", GeminiModel(CHEAP_MODEL), attempts=2),
            Provider("gemma-open", GeminiModel(GEMMA_MODEL), attempts=1),
        ]
    )


#: The pace /desk keeps when the caller names none. A desk snap is a full
#: screenshot through a vision model: not a per-utterance call, so it stays on
#: the board-run pacer at the conservative floor.
DESK_QUOTA_RPM = 15

#: The pace every /transcribe call keeps, wake spotting and push-to-talk
#: dictation alike.
#:
#: Voice is not a board run and must not be paced like one. It is one short
#: clip through ``CHEAP_MODEL`` with a single attempt, it costs about a second,
#: and a human is sitting there waiting for it; a board run is minutes of
#: expensive calls nobody is watching keystroke-by-keystroke. Pacing the two
#: together cost measurably: at 15 RPM every spoken turn that followed another
#: paid a 4 s ``time.sleep``, and because dictation shared
#: ``GEMINI_REQUEST_PACER`` a dictation that followed a 3 RPM board run waited
#: 19.6 s (measured 2026-09-07 on a 4.5 s clip).
#:
#: The pacer itself stays rather than being deleted. It is the only burst
#: ceiling this process has -- a wedged client re-arming its ear in a loop is a
#: real failure mode -- and ``quota.wait`` is what makes a deliberate delay
#: visible instead of looking like a hung call. What changes is that voice gets
#: its own pacer and its own generous floor: 60 RPM is one second, which a
#: model call of roughly that length already covers, so a person speaking turn
#: after turn never waits on the gate, while a runaway loop still hits a stated
#: ceiling. Lower this one number if a free-tier key starts answering 429.
VOICE_QUOTA_RPM = 60

#: Separate from ``GEMINI_REQUEST_PACER`` in both directions: an armed ear
#: cannot delay a board run, and a board run cannot delay a spoken turn. Wake
#: and dictation share this one pacer on purpose -- they are the same model
#: tier against the same project quota, and the most either can cost the other
#: is the 1 s interval.
VOICE_REQUEST_PACER = RequestPacer()

#: Why a clip was recorded. Closed on purpose: an unrecognised purpose is a 400
#: rather than a fall-through to whatever pacer the ``else`` branch happens to
#: name, which is exactly how dictation ended up on the board-run floor.
TRANSCRIBE_PURPOSES = frozenset({"wake", "dictate"})

#: A caller that names no purpose is dictating. /transcribe is a voice route
#: and has no non-voice caller, so the default belongs inside the vocabulary
#: rather than outside it.
DEFAULT_TRANSCRIBE_PURPOSE = "dictate"


def build_transcribe_model():
    """The cheapest Gemini tier the catalog exposes, one attempt, no ladder.

    Transcribing four seconds of audio to look for one word is not worth the
    primary tier, and a 429 must not fan out into five attempts the way the
    board pipeline's failover does: a failed window is one missed window,
    and the next one is already recording.
    """
    from silkscreen.agents.model import CHEAP_MODEL

    return FallbackModel(
        providers=[Provider("gemini-cheap", GeminiModel(CHEAP_MODEL), attempts=1)]
    )


def build_desk_model():
    """Same cheap single-attempt tier as /transcribe.

    A pointing phrase is one vision call, not a board run: the primary
    failover ladder would spend five attempts on a "what's this" and
    starve the generate it exists to keep off the path.
    """
    return build_transcribe_model()


def build_tinker_model():
    """Load the promoted small-policy checkpoint, never an untrained base model."""
    checkpoint = os.getenv("TINKER_PLACEMENT_MODEL", "").strip()
    if not checkpoint:
        raise ValueError(
            "tinker policy is not configured; set TINKER_PLACEMENT_MODEL to "
            "the promoted tinker:// sampler checkpoint"
        )
    if not checkpoint.startswith("tinker://"):
        raise ValueError("TINKER_PLACEMENT_MODEL must be a tinker:// checkpoint")
    from silkscreen.placement.tinker_policy import TinkerPlacementModel

    return TinkerPlacementModel(model_path=checkpoint)


def build_ollama_model():
    base_url = os.getenv("OLLAMA_PLACEMENT_URL", "").strip()
    if not base_url:
        raise ValueError(
            "local policy is not configured; set OLLAMA_PLACEMENT_URL to the "
            "private Ollama endpoint"
        )
    from silkscreen.placement.ollama_policy import OllamaPlacementModel

    return OllamaPlacementModel(
        base_url=base_url,
        model=os.getenv("OLLAMA_PLACEMENT_MODEL", "gemma3:4b"),
    )


def build_fast_placement_model():
    if _tinker_configured():
        return build_tinker_model()
    return build_ollama_model()


def _tinker_configured() -> bool:
    checkpoint = os.getenv("TINKER_PLACEMENT_MODEL", "").strip()
    return bool(os.getenv("TINKER_API_KEY") and checkpoint.startswith("tinker://"))


def _experimental_requested(payload: dict[str, Any]) -> bool:
    value = payload.get("experimental_placement", False)
    if not isinstance(value, bool):
        raise RequestError("experimental_placement must be a boolean")
    return value


def placement_policy_status(*, experimental: bool = False) -> dict[str, bool]:
    gemini = bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"))
    tinker = experimental and _tinker_configured()
    ollama = experimental and bool(os.getenv("OLLAMA_PLACEMENT_URL"))
    return {
        "deterministic": True,
        "gemini": gemini,
        "tinker": tinker,
        "ollama": ollama,
        "hybrid": gemini and (tinker or ollama),
    }


def resolve_placement_policy(
    requested: str, available: dict[str, bool] | None = None
) -> str:
    """Resolve the single fast product mode to the best configured backend."""
    status = available if available is not None else placement_policy_status()
    if requested != "fast":
        if requested not in status:
            raise RequestError(f"unknown placement policy {requested!r}")
        if not status[requested]:
            raise RequestError(f"placement policy {requested!r} is not available")
        return requested
    for candidate in ("hybrid", "tinker", "ollama"):
        if status.get(candidate):
            return candidate
    return "deterministic"


def _placement_model_id(policy: str) -> str:
    if policy == "ollama":
        return os.getenv("OLLAMA_PLACEMENT_MODEL", "gemma3:4b")
    if policy == "tinker":
        return os.getenv("TINKER_PLACEMENT_MODEL", "Qwen/Qwen3.5-4B")
    if policy == "hybrid":
        if os.getenv("TINKER_PLACEMENT_MODEL"):
            return os.getenv("TINKER_PLACEMENT_MODEL", "Qwen/Qwen3.5-4B")
        return os.getenv("OLLAMA_PLACEMENT_MODEL", "gemma3:4b")
    return policy


def _trace_consent(payload: dict[str, Any]) -> tuple[bool, str]:
    """Training traces are always an explicit, experimental opt-in."""
    supplied = payload.get("record_trace")
    if "record_trace" in payload and not isinstance(supplied, bool):
        raise RequestError("record_trace must be a boolean")
    if supplied and payload.get("experimental_placement") is not True:
        raise RequestError("record_trace requires experimental placement features")
    origin = "uploaded-board" if "board" in payload else "generated-board"
    return bool(supplied), origin


def _store_failure_traces(
    result: dict[str, Any],
    store: FailureTraceStore,
    *,
    input_origin: str,
) -> list[str]:
    traces = build_failure_traces(
        result,
        model_id=_placement_model_id(str(result.get("policy", ""))),
        input_origin=input_origin,
    )
    return [store.append(trace) for trace in traces]


def _placement_models(
    policy: str,
    gemini_factory: Callable[[], Any],
) -> tuple[Any | None, Any | None]:
    if policy == "gemini":
        return gemini_factory(), None
    if policy == "tinker":
        return build_tinker_model(), None
    if policy == "ollama":
        return build_ollama_model(), None
    if policy == "hybrid":
        return build_fast_placement_model(), gemini_factory()
    return None, None


def _run_placement_policy(
    payload: dict[str, Any],
    policy: str,
    gemini_factory: Callable[[], Any],
) -> dict[str, Any]:
    model, fallback_model = _placement_models(policy, gemini_factory)
    try:
        return repair_request(
            {**payload, "policy": policy},
            model=model,
            fallback_model=fallback_model,
        )
    except ValueError as exc:
        # engine/silkscreen/placement/api.py raises plain ValueError only for
        # request-shape problems (an unknown profile, a malformed feedback
        # object, an unauthenticated shared profile_id, …) -- caller input,
        # so it is translated to RequestError here rather than left to fall
        # through as an internal failure.
        raise RequestError(str(exc)) from exc


def _record_failure_trace_ids(
    payload: dict[str, Any],
    result: dict[str, Any],
    store: FailureTraceStore | None = None,
) -> list[str]:
    consent, input_origin = _trace_consent(payload)
    if not consent:
        return []
    attempted = result.get("policy_attempt")
    trace_result = attempted if isinstance(attempted, dict) else result
    selected_store = store or build_failure_trace_store()
    try:
        return _store_failure_traces(
            trace_result,
            selected_store,
            input_origin=input_origin,
        )
    except Exception as exc:
        # Training telemetry must never take down board repair.
        sys.stderr.write(
            "placement trace write failed: "
            f"{type(exc).__name__}: {exc}\n"
        )
        return []


def run_chat_orchestrator(**kwargs):
    """Load ADK only for the route that needs its LLM agent."""
    try:
        from silkscreen.agents.adk.orchestrator import run_orchestrator
    except ImportError as exc:
        raise RuntimeError(
            "the chat orchestrator needs the adk extra: pip install 'silkscreen[adk]'"
        ) from exc
    return run_orchestrator(**kwargs)


class _PacedModel:
    """Apply a pre-call hook to a non-fallback model injected into the service."""

    def __init__(self, model, before_attempt: Callable[[str], None]) -> None:
        self._model = model
        self._before_attempt = before_attempt

    @property
    def last_provider(self):
        return getattr(self._model, "last_provider", None)

    @property
    def last_model(self):
        return getattr(self._model, "last_model", None)

    def generate(self, prompt: str, **kwargs):
        self._before_attempt("worker")
        return self._model.generate(prompt, **kwargs)


def _with_request_pacing(model, before_attempt: Callable[[str], None]):
    """Pace every explicit fallback attempt, or one ordinary model call."""
    if isinstance(model, FallbackModel):
        return replace(model, before_attempt=before_attempt)
    return _PacedModel(model, before_attempt)


def _spec_review_block(
    result: Any,
    *,
    intent: str,
    model,
    emit: Callable[[dict[str, Any]], None],
) -> tuple[dict[str, Any] | None, list[str]]:
    """The one-shot route's agenda, through the step path's door.

    ``steps._agenda`` owns the shape (the flattened ``SpecReview``, or None),
    the ref vocabulary (``steps._known_refs``: part refs *and* net names) and
    the evidence it shows the model (findings, unrouted nets, failed kernel
    clauses and sourcing gaps), and it is
    written against a :class:`~service.steps.Session`. A finished
    ``PipelineResult`` is presented as one -- the ``SessionResult`` trick
    ``service/deliver.py`` uses in the other direction -- so the two routes
    cannot drift: the same evidence goes in, the same block comes out, and
    with no evidence at all ``propose_spec_review`` makes no model call.

    The findings and the board are the product; the agenda is a convenience
    on top. So a failure here is ``None`` plus a warning naming why, never a
    500, exactly as the review step answers it.
    """
    session = _steps.Session(
        id="",
        intent=intent,
        stem="",
        directory=Path(),
        kicad_live=False,
        time_limit_s=None,
        spec=result.spec,
        board=result.board,
        route=result.route,
        findings=list(result.findings),
        # The case's kernel receipt and the BOM, when the run produced them,
        # so the agenda sees the same evidence the review step's does. Both
        # are already collected here (the pipeline joined its jobs), so no
        # peek and no wait; ``getattr`` because a result without either
        # field is a run that never asked for it.
        enclosure=getattr(result, "enclosure", None),
        sourcing=getattr(result, "sourcing", None),
    )
    try:
        return _steps._agenda(
            session, model=model, emit=emit, enter=lambda stage: None
        )
    except Exception as exc:  # noqa: BLE001 -- the board is the product
        return None, [
            f"the spec-review agenda could not be prepared: "
            f"{type(exc).__name__}: {exc}"
        ]


def generate(
    payload: dict[str, Any],
    *,
    model,
    store: FactStore,
    pages_store: FactStore | None = None,
    embedder_factory: Callable[[], Any] | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    placement_policy: str = "deterministic",
    placement_model=None,
    placement_fallback_model=None,
) -> dict[str, Any]:
    """Run the pipeline for one request body.

    ``on_event`` is handed to the pipeline unchanged and additionally receives
    the grounding events this function owns, since grounding happens after the
    pipeline has returned and the pipeline therefore cannot report it. Its
    ``t_s`` counts from this call rather than from the pipeline's own start,
    so the two clocks differ by the cache reads done before the pipeline is
    entered; that gap is real and reporting one clock as the other would only
    hide it. Without ``on_event`` nothing is emitted and the response is
    exactly what it was.
    """
    started = time.monotonic()

    def emit(event: dict[str, Any]) -> None:
        if on_event is None:
            return
        event["t_s"] = round(time.monotonic() - started, 3)
        on_event(event)

    intent = str(payload.get("intent") or "").strip()
    if not intent:
        raise RequestError("'intent' is required")

    # An approved manifest is caller-owned input, so reject it before cache
    # reads, datasheet downloads, or a model call can spend time or quota.
    # parse_constraint_manifest (engine/silkscreen/constraints.py) validates
    # the manifest the caller sent and raises plain ValueError -- caller
    # input, not an internal invariant -- so it is translated to RequestError
    # here rather than left to fall through as an internal failure.
    try:
        constraint_manifest = parse_constraint_manifest(payload.get("constraints"))
    except ValueError as exc:
        raise RequestError(str(exc)) from exc
    model_intent = intent
    if constraint_manifest is not None:
        model_intent += constraint_manifest.prompt_block()

    # Enclosure opt-in, validated before any cache read or model call spends
    # time or quota -- field-level failures are the caller's to fix (the
    # known-issue-10 taxonomy: a plain 400, never a stream frame apology).
    enclosure_requested = payload.get("enclosure", False)
    if not isinstance(enclosure_requested, bool):
        raise RequestError("'enclosure' must be a boolean")
    enclosure_style = payload.get("enclosure_style", "")
    if not isinstance(enclosure_style, str):
        raise RequestError("'enclosure_style' must be a string")
    if len(enclosure_style) > MAX_ENCLOSURE_STYLE_CHARS:
        raise RequestError(
            "'enclosure_style' must be at most "
            f"{MAX_ENCLOSURE_STYLE_CHARS} characters"
        )
    enclosure_rigorous = payload.get("enclosure_rigorous", False)
    if not isinstance(enclosure_rigorous, bool):
        raise RequestError("'enclosure_rigorous' must be a boolean")
    # Sourcing opt-in, under the same rule.
    sourcing_requested = payload.get("sourcing", False)
    if not isinstance(sourcing_requested, bool):
        raise RequestError("'sourcing' must be a boolean")
    # SPICE verification opt-in, under the same rule.
    simulate_requested = payload.get("simulate", False)
    if not isinstance(simulate_requested, bool):
        raise RequestError("'simulate' must be a boolean")
    # The thinking level, from the frozen vocabulary. Validated here, before
    # anything spends time or quota, and never defaulted on a bad name: a run
    # that answers a 'thorough' request at 'fast' while reporting 'thorough'
    # is exactly what the level exists to prevent, so an unknown name is the
    # caller's 400.
    effort = payload.get("effort")
    if effort is not None and not isinstance(effort, str):
        raise RequestError("'effort' must be a string")
    try:
        profile_for(effort)
    except ValueError as exc:
        raise RequestError(str(exc)) from exc
    # The spec-review agenda, under the same rule and through the same door
    # as the step path: ``steps._wants_agenda`` reads both spellings --
    # ``{"spec_review": true}`` and the desktop's ``{"summary": "structured"}``
    # -- and defaults off, because an agenda is a model call nobody pressed
    # for. It raises a plain ValueError on a bad field (caller input, not an
    # invariant), translated here so it answers as a 400 rather than a 500.
    # Both fields are presentation settings: they never reach ``generate_pcb``
    # below, which is called with explicit keywords, never ``**payload``.
    try:
        wants_agenda = _steps._wants_agenda(payload)
    except ValueError as exc:
        raise RequestError(str(exc)) from exc

    datasheets = payload.get("datasheets") or {}
    if not isinstance(datasheets, dict):
        raise RequestError("'datasheets' must be an object of {part: url}")
    if any(not isinstance(u, str) or not u for u in datasheets.values()):
        raise RequestError("each datasheet value must be a non-empty URL string")
    order_opts = None
    if payload.get("order") is not None:
        # Validated here, before the pipeline spends a model call: a rejected
        # option is the caller's to fix and should cost them nothing.
        order_opts = order_options(payload["order"])

    if payload.get("ground") is True:
        if not datasheets:
            raise RequestError("'ground' requires 'datasheets'")
        if len(datasheets) > MAX_GROUND_PARTS:
            raise RequestError(
                f"'ground' supports at most {MAX_GROUND_PARTS} datasheets per request"
            )
        for url in datasheets.values():
            shape = urlsplit(url)
            if shape.scheme.lower() not in ("http", "https") or not shape.hostname:
                raise RequestError("datasheet URL is not an http(s) URL")

    # Anything already in Firestore is not read again -- but the facts we
    # stored are handed to the pipeline in the read's place. Skipping the read
    # without supplying the facts would design the board blind, which is
    # strictly worse than not caching at all.
    cached = {p: store.get(p) for p in datasheets}
    to_read = {p: u for p, u in datasheets.items() if cached.get(p) is None}

    preloaded: list[PartFacts] = []
    unusable: list[str] = []
    for part, raw in cached.items():
        if raw is None:
            continue
        try:
            preloaded.append(PartFacts.from_dict(raw))
        except (TypeError, ValueError):
            # A malformed or legacy entry is a cache miss, not a failed
            # request: fall back to reading the datasheet again.
            unusable.append(part)
            to_read[part] = datasheets[part]

    no_solver_budget = payload.get("no_solver_budget", False)
    if not isinstance(no_solver_budget, bool):
        raise RequestError("'no_solver_budget' must be a boolean")
    if no_solver_budget:
        time_limit_s = None
    elif "time_limit_s" not in payload:
        # Absent, not defaulted: the effort level owns the budget nobody
        # named, and the receipt on the response says which level chose it.
        # Substituting the old 20 s default here made every request an explicit
        # override, so the level could never decide anything.
        time_limit_s = UNSET
    else:
        # Coerce here, not in the route handler: float() raises TypeError on a
        # JSON null and ValueError on a string, and both are this caller's
        # error. Raising RequestError (rather than a bare ValueError) is what
        # keeps this caller mistake a 400 while a pipeline ValueError still
        # falls through to the 500 path.
        try:
            time_limit_s = float(payload["time_limit_s"])
        except (TypeError, ValueError):
            raise RequestError("'time_limit_s' must be a number") from None

    placement_profile = payload.get("placement_profile")
    if placement_profile is not None and (
        not isinstance(placement_profile, str) or not placement_profile.strip()
    ):
        raise RequestError("'placement_profile' must be a non-empty profile name")
    placement_feedback = payload.get("placement_feedback")
    if placement_feedback is not None and not isinstance(placement_feedback, dict):
        raise RequestError("'placement_feedback' must be an object")
    placement_max_turns = payload.get("placement_max_turns", 8)
    if isinstance(placement_max_turns, bool) or not isinstance(
        placement_max_turns, int
    ):
        raise RequestError("'placement_max_turns' must be an integer")

    # Passed only when opted in, so a default request reaches generate_pcb
    # with the exact call it always made (and both drivers stay
    # event-identical by default, per the plan). Sourcing joins the same
    # dict under the same rule.
    opt_in_kwargs: dict[str, Any] = (
        {
            "enclosure": True,
            "enclosure_style": enclosure_style.strip(),
            # Fast by default; the strict repair loop is the caller's opt-in.
            "enclosure_rigorous": enclosure_rigorous,
        }
        if enclosure_requested
        else {}
    )
    if sourcing_requested:
        opt_in_kwargs["sourcing"] = True
    if simulate_requested:
        opt_in_kwargs["simulate"] = True

    result = generate_pcb(
        model,
        model_intent,
        datasheets=to_read,
        preloaded_facts=preloaded,
        time_limit_s=time_limit_s,
        review=bool(payload.get("review", True)),
        on_event=on_event,
        # Debugging a run means reading what the model actually said, so the
        # raw answers join the stream only when the caller asks for them.
        include_responses=bool(payload.get("debug", False)),
        placement_profile=placement_profile,
        placement_policy=placement_policy,
        placement_feedback=placement_feedback,
        placement_model=placement_model,
        placement_fallback_model=placement_fallback_model,
        placement_max_turns=placement_max_turns,
        effort=effort,
        **opt_in_kwargs,
    )

    # The run is finished and paid for by the time we get here, so a cache
    # that is down is a cache miss next time, not a lost board -- the same
    # rule the read half above already follows for an unreadable entry. It
    # does not pass quietly either: every failed write is named in the
    # response's warnings.
    cache_warnings: list[str] = []
    for fact in result.facts:
        part = getattr(fact, "part_number", None)
        if not part:
            continue
        try:
            store.put(part, fact.to_dict())
        except Exception as exc:
            cache_warnings.append(
                f"datasheet facts for {part} were not cached: "
                f"{type(exc).__name__}: {exc}"
            )

    board = result.board
    refs = _refs_by_spec_name(result.spec, board)
    response: dict[str, Any] = {
        "intent": intent,
        "board_mm": [round(to_mm(board.width_nm), 3), round(to_mm(board.height_nm), 3)],
        "status": str(board.solver_status),
        "parts": [{"ref": p.ref, "footprint": p.footprint.name} for p in board.parts],
        "kicad_pcb": emit_kicad_pcb(board),
        "repair_rounds": result.repair_rounds,
        "blockers": [str(b) for b in result.blockers],
        "findings": [_finding_dict(f, refs) for f in result.findings],
        # The same block the review step puts on its envelope, from the
        # same builder: ``findings`` alone cannot say whether an empty list
        # is a clean board, a critic that was never asked, or an answer that
        # could not be read, and only ``status: ok`` makes it the first.
        "review": _steps.review_block(getattr(result, "review", None)),
        "duration_s": round(time.monotonic() - started, 3),
        # Which effort level produced this board, and what that level gave
        # the run. Always present, never null: the default level is ``fast``,
        # which places inside a quarter of balanced's solver budget, and a
        # client that renders a verdict has to be able to say which level it
        # is looking at. ``degraded`` is the boolean to branch on and
        # ``headline`` the sentence to print.
        "effort": (
            None if result.effort is None else result.effort.as_dict()
        ),
        "warnings": list(board.warnings) + cache_warnings,
        "nets": list(board.nets),
        # Both freshly read and cache-supplied facts land in result.facts, so
        # this reports what the design was actually informed by.
        "datasheets": [
            {
                "part": f.part_number,
                "package": f.package,
                "pins": len(f.pins),
                "requirements": len(f.requirements),
                "url": f.source_url,
            }
            for f in result.facts
        ],
        # A "hit" is an entry we could actually use. An entry that was present
        # but unreadable is reported as a miss and a re-read, because that is
        # what happened.
        "cache": {
            "hit": sorted(
                p for p, v in cached.items() if v is not None and p not in unusable
            ),
            "read": sorted(to_read),
            "unusable": sorted(unusable),
        },
        "served_by": getattr(model, "last_provider", None),
        # Geometry, resolved server-side. Additive: nothing above changes
        # shape, so a client that only reads "parts" keeps working.
        "placements": _placements_dict(board),
        # Electrical topology, separate from physical placement.  The browser
        # lays this out as a schematic without having to parse CircuitSpec.
        "schematic": _schematic_dict(result.spec, refs),
        "wirelength_mm": (
            None
            if board.wirelength_nm is None
            else round(to_mm(board.wirelength_nm), 3)
        ),
    }

    if constraint_manifest is not None:
        receipt = verify_constraint_manifest(
            constraint_manifest,
            result.spec,
            board,
            result.route,
        )
        checks = [
            check
            for group in receipt.get("net_classes", [])
            for check in group.get("checks", [])
        ]
        checks.extend(receipt.get("mechanical", []))
        counts = {
            status: sum(check.get("status") == status for check in checks)
            for status in ("verified", "violated", "unresolved")
        }

        response["constraint_manifest"] = constraint_manifest.to_dict()
        response["constraint_receipt"] = receipt
        # This is production-promotion eligibility metadata, not an artifact
        # gate. The generated KiCad board stays available in this response.
        response["promotion_status"] = (
            "constraint_passed" if receipt["promotable"] else "constraint_blocked"
        )
        response["blockers"].extend(
            f"constraint {item['scope']}/{item['name']}: {item['detail']}"
            for item in receipt["blockers"]
        )
        emit(
            {
                "event": "constraints.verify",
                "manifest_version": constraint_manifest.version,
                "hard_gate": receipt["hard_gate"],
                "promotable": receipt["promotable"],
                "blockers": len(receipt["blockers"]),
                "verified": counts["verified"],
                "violated": counts["violated"],
                "unresolved": counts["unresolved"],
                "artifact_available": True,
            }
        )

    if enclosure_requested:
        # Additive, and only when opted in. ``getattr`` rather than an
        # attribute read: enclosure failure inside the stage already means
        # ``None`` (board still delivered), and the visible warning keeps the
        # degradation honest in the one-shot response, where the
        # ``enclosure.failed`` stream event cannot be seen.
        response["enclosure"] = _enclosure_dict(getattr(result, "enclosure", None))
        if response["enclosure"] is None:
            response["warnings"].append(
                "enclosure generation failed; the board is delivered without a case"
            )

    if sourcing_requested:
        # Additive, and only when opted in. The stage itself never answers
        # None -- a failure degrades to the deterministic rows plus a warning
        # inside ``sourcing.warnings`` -- so None here means the pipeline
        # that answered did not run the stage at all, and the one-shot
        # response says so in place of the ``sourcing.failed`` stream event.
        sourcing = getattr(result, "sourcing", None)
        response["sourcing"] = sourcing.as_dict() if sourcing is not None else None
        if sourcing is None:
            response["warnings"].append(
                "sourcing did not run; the board is delivered without a BOM"
            )

    if simulate_requested:
        # Additive, and only when opted in. The stage itself never answers
        # None -- no simulator, a part with no model, no usable testbench
        # and a simulator that raised are all statuses on the result, and a
        # failed clause is a finding inside it -- so None here means the
        # pipeline that answered did not run the stage at all, and the
        # one-shot response says so in place of the stream's events.
        simulation = getattr(result, "simulation", None)
        response["simulation"] = (
            simulation.as_dict() if simulation is not None else None
        )
        if simulation is None:
            response["warnings"].append(
                "simulation did not run; the board is delivered unverified"
            )

    if result.route is not None:
        response["routing"] = {
            "tracks": len(result.route.tracks),
            "vias": len(result.route.vias),
            "routed": list(result.route.routed),
            "unrouted": dict(result.route.unrouted),
            "warnings": list(result.route.warnings),
            "completion": round(result.route.completion, 4),
        }

    if wants_agenda:
        # Additive, and only when asked for. The block is the same flattened
        # ``SpecReview`` the review step puts on its envelope (``title`` /
        # ``summary`` / ``items`` / ``total_minutes`` / ``needs_meeting``, or
        # null plus a warning saying why), because the desktop's
        # ``specReviewFrom`` reads one shape off both routes. It sits above
        # the ``ground`` branch below on purpose: that branch returns early.
        block, agenda_warnings = _spec_review_block(
            result, intent=intent, model=model, emit=emit
        )
        response["spec_review"] = block
        response["warnings"].extend(agenda_warnings)

    if result.placement is not None:
        placement = run_to_dict(result.placement.run, placement_feedback)
        placement["requested_policy"] = result.placement.requested_policy
        placement["applied"] = result.placement.applied
        placement["policy_fallback"] = result.placement.policy_fallback
        if result.placement.attempted_run is not None:
            placement["policy_attempt"] = run_to_dict(
                result.placement.attempted_run,
                placement_feedback,
            )
        response["placement_repair"] = placement

    if order_opts is not None:
        response["order"] = _order_block(board, result.spec, order_opts)

    if payload.get("ground") is True:
        if not result.findings:
            response["grounding"] = {
                "findings": [],
                "pages": {"cached": [], "read": []},
            }
            return response

        pages_store = pages_store if pages_store is not None else build_pages_store()
        embedder = (embedder_factory or build_embedder)()

        indexes: dict[str, Any] = {}
        pages_cached: list[str] = []
        pages_read: list[str] = []
        for part, url in datasheets.items():
            pages = load_pages(pages_store, page_cache_key(part, url))
            # Read before the fetch below reassigns it: afterwards every part
            # holds pages and the distinction that matters is gone.
            cached = pages is not None
            if pages is None:
                try:
                    pages = pages_for_part(url=url)
                except ValueError as exc:
                    sys.stderr.write(f"grounding rejected datasheet url: {exc}\n")
                    raise RequestError("datasheet URL is not allowed") from exc
                store_pages(pages_store, page_cache_key(part, url), pages)
                pages_read.append(part)
            else:
                pages_cached.append(part)
            emit({"event": "ground.part", "part": part, "cached": cached})
            index = build_index(pages, embedder)
            if len(index):
                indexes[part] = index

        grounded = ground_findings(indexes, result.findings)
        response["grounding"] = {
            "findings": [
                {
                    "severity": g.finding.severity.value,
                    "title": g.finding.title,
                    "detail": g.finding.detail,
                    "parts": list(g.finding.parts),
                    "citation": g.finding.citation,
                    "suggested_fix": g.finding.suggested_fix,
                    "status": g.status.value,
                    "evidence": [
                        {
                            "part": e.part,
                            "page": e.page,
                            "score": round(e.score, 4),
                            "quote": e.quote,
                        }
                        for e in g.evidence
                    ],
                }
                for g in grounded
            ],
            "pages": {"cached": sorted(pages_cached), "read": sorted(pages_read)},
        }
    return response


class Handler(BaseHTTPRequestHandler):
    """Same-origin chat, generation, placement repair, and built web UI."""

    model_factory = staticmethod(build_model)
    transcribe_model_factory = staticmethod(build_transcribe_model)
    desk_model_factory = staticmethod(build_desk_model)
    model_catalog_factory = staticmethod(model_catalog)
    configuration_status_factory = staticmethod(configuration_status)
    orchestrator_runner = staticmethod(run_chat_orchestrator)
    request_pacer: RequestPacer = GEMINI_REQUEST_PACER
    voice_pacer: RequestPacer = VOICE_REQUEST_PACER
    store: FactStore | None = None
    pages_store: FactStore | None = None
    failure_trace_store: FailureTraceStore | None = None
    embedder_factory = staticmethod(build_embedder)

    #: How ``/speak`` gets its voices. A factory rather than an instance, the
    #: ``model_factory`` convention: a test swaps in engines with a recorded
    #: transport and the suite never opens a socket or loads a model.
    tts_engines_factory = staticmethod(_tts.build_engines)

    #: Root of the built bundle; None serves no static files at all.
    web_root: Path | None = WEB_DIST

    # There are deliberately no CORS headers and no do_OPTIONS: the bundle is
    # served from this same origin, so nothing the UI sends is cross-origin.
    # Adding them defensively would only widen who may call /generate.

    def _send(
        self,
        code: int,
        payload: dict[str, Any],
        *,
        cache_control: str | None = None,
        close: bool = False,
        run_id: str | None = None,
    ) -> None:
        try:
            body = json.dumps(payload, allow_nan=False).encode()
        except (TypeError, ValueError) as exc:
            error_id = uuid.uuid4().hex[:12]
            sys.stderr.write(
                f"error {error_id}: response serialization failed: {exc}\n"
            )
            code = 500
            body = json.dumps(
                {"error": "internal error", "error_id": error_id}
            ).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if run_id:
            # Also on the header, so it survives a body this client never
            # manages to parse -- the reason LiteLLM's proxy puts its own call
            # id on `x-litellm-call-id` rather than only in the payload.
            self.send_header("X-Kaleo-Run-Id", run_id)
        if cache_control:
            self.send_header("Cache-Control", cache_control)
        if close:
            # Announced as well as done: a keep-alive client that pipelined a
            # second request into a socket we are about to close would see a
            # reset instead of an answer.
            self.close_connection = True
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, code: int, page: str) -> None:
        """One HTML page (the demo consent screen), locked down.

        No caching, no framing, no referrer, no sniffing, and a CSP that
        allows the page's own inline styles and a form back to this origin
        and nothing else -- it is the one thing this service serves to a
        browser tab that is not the bundle.
        """
        body = page.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'",
        )
        self.end_headers()
        self.wfile.write(body)

    def _read_form(self, *, max_bytes: int = MAX_FORM_BYTES) -> dict[str, str] | None:
        """A small ``application/x-www-form-urlencoded`` body, or None once
        its error is sent. Only the demo consent POST reads one."""
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if content_type.lower() != "application/x-www-form-urlencoded":
            self._send(415, {"error": "expected application/x-www-form-urlencoded"})
            return None
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send(400, {"error": "invalid Content-Length"})
            return None
        if length < 0:
            self._send(400, {"error": "invalid Content-Length"})
            return None
        if length > max_bytes:
            self._send(413, {"error": "form body too large"})
            return None
        raw = self.rfile.read(length) if length else b""
        pairs = urllib.parse.parse_qs(
            raw.decode("utf-8", "replace"), keep_blank_values=True
        )
        return {key: values[0] for key, values in pairs.items() if values}

    def _setup_base_url(self) -> str:
        """Where the demo consent link points: the Host the client used, if it
        is loopback, else this server's own loopback address."""
        host = self.headers.get("Host")
        if host and _setup.host_is_local(host, self.server.server_address[0]):
            return f"http://{host}"
        return f"http://127.0.0.1:{self.server.server_port}"

    def _setup_get(self, route: str) -> None:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        flat = {key: values[0] for key, values in query.items() if values}
        try:
            status, body, content_type = _setup.handle_get(
                route,
                flat,
                host=self.headers.get("Host"),
                server_host=self.server.server_address[0],
            )
        except Exception as exc:  # noqa: BLE001 - sanitised like every route
            status, body = _error_response(exc)
            content_type = "application/json"
        if content_type == "text/html":
            self._send_html(status, body)
        else:
            self._send(status, body, cache_control="no-store")

    def _setup_post(self, route: str) -> None:
        form = None
        payload: dict[str, Any] | None = {}
        if route == _setup.CONSENT_ROUTE:
            # The form is read before any JSON parse is attempted: a browser
            # form post is not JSON and must not be answered "invalid JSON".
            form = self._read_form()
            if form is None:
                return
        else:
            payload = self._read_payload()
            if payload is None:
                return
        try:
            status, body, content_type = _setup.handle_post(
                route,
                payload,
                base_url=self._setup_base_url(),
                form=form,
                host=self.headers.get("Host"),
                server_host=self.server.server_address[0],
            )
        except Exception as exc:  # noqa: BLE001 - sanitised like every route
            status, body = _error_response(exc)
            content_type = "application/json"
        if content_type == "text/html":
            self._send_html(status, body)
        else:
            self._send(status, body, cache_control="no-store")

    def _authorized(self, route: str, *, static_ok: bool = False) -> bool:
        """True when this request may proceed past the bearer gate.

        With no ``SILKSCREEN_ACCESS_TOKEN`` in the environment there is no
        gate at all, which is the local contract: nothing about running the
        service on loopback changes. With one set, every route but
        :data:`AUTH_EXEMPT_ROUTES` (and, on GET, the built bundle) needs
        ``Authorization: Bearer <token>`` -- the header the desktop client
        already sends.

        The comparison is :func:`hmac.compare_digest` over bytes, never ``==``:
        a byte-at-a-time comparison leaks the token's prefix to anyone willing
        to time enough requests, and this token is the only thing standing
        between a public URL and the owner's Gemini bill.
        """
        expected = access_token()
        if not expected:
            return True
        if route in AUTH_EXEMPT_ROUTES:
            return True
        if _setup.is_public_demo_route(route):
            # The demo consent page is opened by a browser tab that cannot
            # send the bearer; it is gated by a single-use nonce instead, and
            # only exists in demo mode (service/setup.py).
            return True
        if static_ok and (route == "/" or self._resolve_static(route) is not None):
            return True
        scheme, _, presented = (self.headers.get("Authorization") or "").partition(" ")
        if scheme.strip().lower() != "bearer":
            return False
        return hmac.compare_digest(
            presented.strip().encode("utf-8"), expected.encode("utf-8")
        )

    def _unauthorized(self) -> None:
        """Refuse one request, and close rather than keep the connection.

        The request body is deliberately never read on this path -- a rejected
        caller must not get to stream a megabyte into the process -- so the
        connection cannot be reused: whatever is unread would be parsed as the
        next request line.
        """
        self._send(
            401,
            {"error": "unauthorized: this service requires a bearer token"},
            cache_control="no-store",
            close=True,
        )

    def _resolve_static(self, route: str) -> Path | None:
        """The bundle file ``route`` names, or None if it names none.

        Two independent defences, because each covers what the other misses.
        The segment whitelist runs on the *decoded* path, so ``%2e%2e%2f`` and
        the Windows ``..%5c`` are refused before any filesystem call; the
        resolve/relative_to containment catches a symlink pointing out of the
        bundle, which no string check can see.
        """
        root = self.web_root
        if root is None:
            return None

        segments = urllib.parse.unquote(route).lstrip("/").split("/")
        if segments == [""]:
            segments = ["index.html"]
        for segment in segments:
            if segment in ("", ".", "..") or "\\" in segment or ":" in segment:
                return None

        try:
            resolved = root.joinpath(*segments).resolve()
            resolved.relative_to(root.resolve())
        except (OSError, ValueError):
            return None
        return resolved if resolved.is_file() else None

    def _is_fingerprinted(self, path: Path) -> bool:
        """True only for files the build named with a content hash.

        The bundler puts those in ``assets/``. Everything else -- index.html,
        and anything copied verbatim from ``frontend/public`` -- keeps a fixed name
        across deploys, so an immutable year on it is a cache entry with no
        way to be busted short of a new URL.
        """
        root = self.web_root
        if root is None:
            return False
        try:
            relative = path.resolve().relative_to(root.resolve())
        except (OSError, ValueError):
            return False
        return len(relative.parts) > 1 and relative.parts[0] == "assets"

    def _send_file(self, path: Path) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self._send(404, {"error": f"no route {self.path}"})
            return
        self.send_response(200)
        self.send_header(
            "Content-Type",
            _CONTENT_TYPES.get(path.suffix.lower(), _DEFAULT_CONTENT_TYPE),
        )
        self.send_header("Content-Length", str(len(body)))
        if self._is_fingerprinted(path):
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        else:
            self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        route = urllib.parse.urlsplit(self.path).path

        # The probe is answered before the bundle is consulted, so a build
        # output file named "healthz" can never shadow the check Cloud Run
        # uses to decide whether this revision is alive. /readyz exists because
        # Google's frontend intercepts /healthz on run.app domains at the edge
        # (404, request never reaches the container), so an external smoke
        # check needs a name the frontend leaves alone.
        if route in ("/healthz", "/readyz"):
            self._send(200, {"ok": True, "service": "silkscreen"})
            return

        # Every other GET is gated once a token is configured, the bundle
        # excepted (``static_ok``): /models spends a live listModels call,
        # /config/status and /integrations describe the deployment, and /steps
        # hands back somebody's run.
        if not self._authorized(route, static_ok=True):
            self._unauthorized()
            return

        if route == "/setup" or route.startswith("/setup/"):
            # The Setup Assistant's report and the demo consent page
            # (service/setup.py). Before the bundle: a build output file
            # named "setup" must never shadow it.
            self._setup_get(route)
            return

        if _inbox.is_inbox_path(route):
            # Ideas sent from Slack, waiting for the overlay (service/inbox.py).
            # Never cached: the poll exists because the answer changes.
            status, body = _inbox.handle_get(route)
            self._send(status, body, cache_control="no-store")
            return

        if route == "/runs" or route.startswith("/runs/"):
            self._runs_get(route)
            return

        if route == "/billing/config":
            status, body = _billing.handle_config_get()
            self._send(status, body, cache_control="no-store")
            return

        if route == "/billing/connect":
            status, body = _billing.handle_connect_status()
            self._send(status, body, cache_control="no-store")
            return

        if route == "/billing/balance":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            account = (query.get("account") or [None])[0]
            status, body = _billing.handle_balance(account)
            self._send(status, body, cache_control="no-store")
            return

        if route == "/models":
            catalog = dict(self.model_catalog_factory())
            catalog["placement"] = {
                # The visible request toggle is the opt-in. Individual providers
                # remain unavailable until their server-side configuration exists.
                "experimental_enabled": True,
                "profiles": ["compact-control", "thermal-first"],
                "policies": placement_policy_status(experimental=True),
            }
            self._send(200, catalog)
            return

        if route == "/config/status":
            self._send(
                200,
                self.configuration_status_factory(),
                cache_control="no-store",
            )
            return

        if route == "/deliver/config":
            # What the delivery routes could reach right now; never a secret
            # (service/deliver.py), and never cached -- a token can expire
            # between two looks.
            self._send(200, _deliver.handle_get(route), cache_control="no-store")
            return

        if route == "/speak":
            # Which voice would answer a POST here, and the named reason every
            # other engine would not. Read-only, never a live call, and
            # no-store for the /integrations reason: a key can be exported
            # between two looks.
            self._speak_report()
            return

        if route == "/integrations":
            # Every front end and optional tool, in one read-only view
            # (service/integrations.py): never a secret, never a live call, and
            # never cached -- a key can be exported between two looks. The
            # module answers for every integration even when one probe fails,
            # so this arm has no error path of its own.
            self._send(
                200,
                _integrations.integrations_report(),
                cache_control="no-store",
            )
            return

        if route.startswith("/steps"):
            try:
                body = _steps.handle_get(route)
            except _steps.StepNotFound as exc:
                self._send(404, {"error": str(exc)})
            else:
                self._send(200, body, cache_control="no-store")
            return

        static = self._resolve_static(route)
        if static is not None:
            self._send_file(static)
            return

        # No blanket SPA fallback: the UI's tabs are hash fragments that never
        # reach the server, so a miss here is a genuinely missing file, and
        # answering it with index.html turns that into a blank page.
        root = self.web_root
        if route == "/" and (root is None or not (root / "index.html").is_file()):
            self._send(200, {"ok": True, "service": "silkscreen"})
            return

        self._send(404, {"error": f"no route {self.path}"})

    def do_POST(self) -> None:
        # Before the body is read, and before any route runs: a POST is where
        # the money is spent. No ``static_ok`` here -- POST never serves a file,
        # and POST "/" is the /generate alias.
        if not self._authorized(urlsplit(self.path).path):
            self._unauthorized()
            return
        if urlsplit(self.path).path.startswith("/setup/"):
            # Matched on the path, not on self.path: the consent form posts to
            # a bare path, but a client may add a query string.
            self._setup_post(urlsplit(self.path).path)
            return
        if self.path == "/placement/repair":
            self._placement_repair()
            return
        if self.path == "/chat/stream":
            self._chat_stream()
            return
        if self.path == "/generate/stream":
            self._generate_stream()
            return
        if self.path in ("/generate", "/"):
            self._generate_once()
            return
        if self.path == "/speak":
            self._speak()
            return

        if self.path == "/transcribe":
            self._transcribe()
            return
        if self.path == "/desk/resolve":
            self._desk_resolve()
            return
        if self.path in ("/deliver/auth", "/deliver/auth/start"):
            # /start returns the consent URL for Ada to open; /auth finishes
            # (client_opens) or runs the all-in-one server-side browser flow.
            payload = self._read_payload()
            if payload is None:
                return
            try:
                body = _deliver.handle_post(self.path, payload)
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
            except Exception as exc:
                status, body = _error_response(exc)
                self._send(status, body)
            else:
                self._send(200, body, cache_control="no-store")
            return
        if _inbox.is_inbox_path(urlsplit(self.path).path):
            # Stores or hands off an idea; never runs one (service/inbox.py).
            payload = self._read_payload()
            if payload is None:
                return
            status, body = _inbox.handle_post(urlsplit(self.path).path, payload)
            self._send(status, body, cache_control="no-store")
            return
        if urlsplit(self.path).path.startswith("/runs/"):
            self._runs_post(urlsplit(self.path).path)
            return
        if self.path.startswith("/steps"):
            self._step()
            return
        if self.path == "/billing/webhook":
            self._billing_webhook()
            return
        if self.path == "/billing/config":
            payload = self._read_payload()
            if payload is None:
                return
            status, body = _billing.handle_config_post(payload)
            self._send(status, body, cache_control="no-store")
            return
        if self.path == "/billing/checkout":
            payload = self._read_payload()
            if payload is None:
                return
            status, body = _billing.handle_checkout(payload)
            self._send(status, body, cache_control="no-store")
            return
        if self.path == "/billing/connect":
            # Opens Stripe's consent page in the user's browser and answers
            # 202 straight away; the flow waits on a human, so the UI polls
            # GET /billing/connect rather than holding this request open.
            status, body = _billing.handle_connect_start()
            self._send(status, body, cache_control="no-store")
            return
        if self.path == "/billing/disconnect":
            status, body = _billing.handle_disconnect()
            self._send(status, body, cache_control="no-store")
            return
        self._send(404, {"error": f"no route {self.path}"})

    def _billing_webhook(self) -> None:
        """Stripe's webhook. Reads RAW bytes -- never ``_read_payload``.

        ``_read_payload`` reads and JSON-decodes in one step, and a signature
        checked against a re-serialised object verifies a different string
        than the one Stripe signed. The whole point of this handler is that
        the bytes reaching ``verify_signature`` are the bytes Stripe hashed.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send(400, {"error": "invalid Content-Length"})
            return
        if length < 0:
            self._send(400, {"error": "invalid Content-Length"})
            return
        if length > _billing.MAX_WEBHOOK_BYTES:
            self._send(413, {"error": "webhook body too large"})
            return
        raw = self.rfile.read(length) if length else b""
        status, body = _billing.handle_webhook(
            raw, self.headers.get("Stripe-Signature", "")
        )
        self._send(status, body, cache_control="no-store")

    def _drain_body(self) -> None:
        """Read and discard this request's body, bounded.

        A route that ignores its body still has to consume it: unread bytes
        sit in the socket and turn this server's connection close into a reset
        the client reads as a network failure rather than as the answer it was
        actually given. The desktop's `cancelRun` goes through `stepPost`,
        which posts `{}` — two bytes, and enough to do it.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if 0 < length <= MAX_BODY_BYTES:
            with contextlib.suppress(OSError):
                self.rfile.read(length)

    def _new_run(self, route: str) -> _runs.RunRecord:
        """Register this request's run, under the caller's id if it sent one.

        ``X-Kaleo-Run-Id`` inbound is optional and is LiteLLM's
        ``x-litellm-call-id`` (read in
        ``ProxyBaseLLMRequestProcessing.common_processing_pre_call_logic``,
        returned by ``get_custom_headers``). Reusing an id raises rather than
        replaying: unlike ``steps.start_once``'s ``Idempotency-Key``, this
        registry keeps no result to hand back, so a "replay" would report some
        other run's outcome as this caller's.
        """
        return _runs.new_run(
            route, run_id=(self.headers.get("X-Kaleo-Run-Id", "") or "").strip()
        )

    def _runs_get(self, route: str) -> None:
        """``GET /runs`` and ``GET /runs/<id>`` -- poll a streamed run.

        This is the rejoin half of ``docs/paid-run-safety.md`` §6. It answers
        the two questions a client that lost its stream actually has: is the
        run I paid for still going, and what did it cost. It does **not** hand
        back the board -- see ``service/runs.py`` for why, and ``/steps`` for
        the surface that does keep artifacts.
        """
        parts = [p for p in route.split("/") if p]
        try:
            if len(parts) == 1:
                body = _runs.snapshot_all()
            elif len(parts) == 2:
                body = _runs.snapshot(parts[1])
            else:
                raise _runs.RunNotFound(f"no route {route}")
        except _runs.RunNotFound as exc:
            self._send(404, {"error": str(exc)}, cache_control="no-store")
            return
        # Never cached: the whole value of this route is that the answer
        # changes while a paid run is in flight.
        self._send(200, body, cache_control="no-store")

    def _runs_post(self, route: str) -> None:
        """``POST /runs/<id>/cancel`` -- ask a streamed run to stop.

        Nothing in the body is *used*: there is nothing to say beyond the id in
        the path, and a cancel that could fail on a malformed body would fail
        exactly when a run is going wrong (``service/amend.py::
        is_lifecycle_path`` makes the same argument for the step routes'
        cancel). It is still drained, because bytes left unread in the socket
        turn this server's connection close into a reset on the client -- the
        reason ``post_stream`` in the tests sends no body when it sends no
        Content-Length.
        """
        self._drain_body()
        parts = [p for p in route.split("/") if p]
        if len(parts) != 3 or parts[2] != "cancel":
            self._send(404, {"error": f"no route {route} (POST /runs/<id>/cancel)"})
            return
        try:
            body = _runs.cancel(parts[1])
        except _runs.RunNotFound as exc:
            self._send(404, {"error": str(exc)}, cache_control="no-store")
            return
        self._send(200, body, cache_control="no-store")

    def _step(self) -> None:
        """One approval-gated step of a run held in memory (service/steps.py)."""
        payload = self._read_payload()
        if payload is None:
            return
        try:
            if _amend.is_lifecycle_path(self.path):
                # /steps/<id>/amend and /steps/<id>/cancel, answered before a
                # model is built. Neither spends a model call, and a cancel
                # button that fails on a machine with no API key would fail
                # exactly when a run is going wrong (service/amend.py).
                body = _amend.handle_post(self.path, payload)
            elif _deliver.is_deliver_path(self.path):
                # /steps/<id>/deliver sends a routed session to Google
                # Workspace (service/deliver.py). No model call, no store: it
                # reads the session and its board file and nothing else.
                body = _deliver.handle_post(self.path, payload)
            else:
                store = self.store if self.store is not None else build_store()
                # Optional, and only ``POST /steps`` reads it: a start has no
                # session to be guarded by, so this header is the one way a
                # caller can say "this is the same press as before" and not be
                # billed for a second read/plan/propose. The spelling and the
                # semantics are Stripe's (stripe-python's
                # ``_api_requestor.request_headers`` sets ``Idempotency-Key``
                # on every POST); service/steps.py::start_once has the rules.
                body = _steps.handle_post(
                    self.path,
                    payload,
                    model=self.model_factory(),
                    store=store,
                    idempotency_key=self.headers.get("Idempotency-Key", "") or "",
                )
        except _steps.StepNotFound as exc:
            self._send(404, {"error": str(exc)})
        except _amend.RunCancelled as exc:
            # The step was abandoned mid-flight because the run was cancelled
            # (service/amend.py). 409 for the same reason StepOrderError is:
            # the request was well-formed and the run's state refused it.
            self._send(409, {"error": str(exc)})
        except _steps.StepOrderError as exc:
            self._send(409, {"error": str(exc)})
        except ValueError as exc:
            # service/steps.py is a separate module from this one and raises
            # plain ValueError only at its own field-validation sites (the
            # same convention as this module's RequestError, just not this
            # module's type) -- out of scope for the RequestError migration,
            # but its existing 400 contract must not regress alongside it.
            self._send(400, {"error": str(exc)})
        except Exception as exc:
            status, body = _error_response(exc)
            self._send(status, body)
        else:
            self._send(200, body)

    def _placement_repair(self) -> None:
        payload = self._read_payload()
        if payload is None:
            return
        try:
            experimental = _experimental_requested(payload)
            requested_policy = str(
                payload.get("policy", "deterministic")
            ).strip().lower()
            policy_status = placement_policy_status(experimental=experimental)
            policy = resolve_placement_policy(requested_policy, policy_status)
            quota_rpm = select_quota_rpm(payload.get("quota_rpm"))

            def gemini_factory():
                model = self.model_factory()
                if quota_rpm is not None:
                    model = _with_request_pacing(
                        model,
                        lambda _provider: self.request_pacer.wait(quota_rpm),
                    )
                return model

            try:
                result = _run_placement_policy(payload, policy, gemini_factory)
            except (OSError, PlacementPolicyError, TimeoutError):
                if requested_policy != "fast" or policy == "deterministic":
                    raise
                unavailable_policy = policy
                policy = "deterministic"
                result = _run_placement_policy(payload, policy, gemini_factory)
                result["policy_fallback"] = {
                    "from": unavailable_policy,
                    "to": policy,
                    "reason": "fast proposer unavailable",
                }
            if (
                requested_policy == "fast"
                and policy != "deterministic"
                and not result.get("completed")
            ):
                incomplete_policy = policy
                attempted = result
                policy = "deterministic"
                result = _run_placement_policy(payload, policy, gemini_factory)
                result["policy_attempt"] = attempted
                result["policy_fallback"] = {
                    "from": incomplete_policy,
                    "to": policy,
                    "reason": "fast proposer did not complete repair",
                }
            result["requested_policy"] = requested_policy
            result["available_policies"] = policy_status
            trace_ids = _record_failure_trace_ids(
                payload, result, self.failure_trace_store
            )
            result["failure_trace_ids"] = trace_ids
            result["failure_trace_count"] = len(trace_ids)
        except Exception as exc:
            status, body = _error_response(exc)
            self._send(status, body)
        else:
            self._send(200, result)

    def _read_payload(
        self, *, max_bytes: int = MAX_BODY_BYTES
    ) -> dict[str, Any] | None:
        """The request body as a JSON object, or None once its error is sent.

        Shared by both POST routes. Everything checked here fails before a
        single byte of response has been written, which is what lets the
        streaming route answer a bad request with a plain JSON error rather
        than a stream whose only frame is an apology.
        """
        # A malformed Content-Length is the client's error. Parsing it outside
        # a guard lets a header of "abc" raise before any response is sent, so
        # the caller sees a dropped connection instead of a 400.
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send(400, {"error": "invalid Content-Length"})
            return None
        if length < 0:
            self._send(400, {"error": "invalid Content-Length"})
            return None
        if length > max_bytes:
            self._send(413, {"error": "request body too large"})
            return None
        try:
            payload = json.loads(
                self.rfile.read(length) or b"{}",
                parse_constant=_reject_nonfinite_json,
                parse_float=_parse_json_float,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            self._send(400, {"error": f"invalid JSON: {exc}"})
            return None
        if not isinstance(payload, dict):
            self._send(400, {"error": "body must be a JSON object"})
            return None
        return payload

    def _run(
        self,
        payload: dict[str, Any],
        on_event: Callable[[dict[str, Any]], None] | None = None,
        quota_rpm: int | None = None,
    ) -> dict[str, Any]:
        """One pipeline run, wired to whatever this handler was injected with."""
        if quota_rpm is None:
            quota_rpm = select_quota_rpm(payload.get("quota_rpm"))
        store = self.store if self.store is not None else build_store()
        model = self.model_factory()
        if quota_rpm is not None:

            def before_attempt(provider: str) -> None:
                self.request_pacer.wait(
                    quota_rpm,
                    on_wait=lambda delay: on_event
                    and on_event(
                        {
                            "event": "quota.wait",
                            "layer": "worker",
                            "provider": provider,
                            "quota_rpm": quota_rpm,
                            "delay_s": round(delay, 3),
                        }
                    ),
                )

            model = _with_request_pacing(model, before_attempt)

        experimental = _experimental_requested(payload)
        trace_consent, _ = _trace_consent(payload)
        placement_profile = payload.get("placement_profile")
        placement_status = placement_policy_status(experimental=experimental)
        placement_policy = "deterministic"
        requested_placement_policy: str | None = None
        placement_model = None
        placement_fallback_model = None
        if placement_profile is not None:
            requested_placement_policy = str(
                payload.get("placement_policy", "deterministic")
            ).strip().lower()
            placement_policy = resolve_placement_policy(
                requested_placement_policy, placement_status
            )
            placement_model, placement_fallback_model = _placement_models(
                placement_policy, lambda: model
            )
        elif trace_consent:
            raise RequestError("record_trace requires placement_profile")

        result = generate(
            payload,
            model=model,
            store=store,
            pages_store=self.pages_store,
            embedder_factory=self.embedder_factory,
            on_event=on_event,
            placement_policy=placement_policy,
            placement_model=placement_model,
            placement_fallback_model=placement_fallback_model,
        )
        placement = result.get("placement_repair")
        if isinstance(placement, dict):
            if requested_placement_policy is not None:
                placement["requested_policy"] = requested_placement_policy
            placement["available_policies"] = placement_status
            placement["experimental_placement"] = experimental
            trace_ids = _record_failure_trace_ids(
                payload, placement, self.failure_trace_store
            )
            placement["failure_trace_ids"] = trace_ids
            placement["failure_trace_count"] = len(trace_ids)
        return result

    def _generate_once(self) -> None:
        """The whole run in one response, once it is finished.

        Addressable for the same reason the streaming route is, even though
        this one has no frames to lose: a client whose connection dies while
        the pipeline is running has still paid for it, and `X-Kaleo-Run-Id`
        plus `GET /runs/<id>` is the only way it can find out what happened.

        The id rides on the **header only** here, never in the body, and the
        body is untouched. That is not squeamishness: this route's body is an
        equality contract with the stream's `run.done` result
        (`test_a_stream_reports_the_run_and_ends_with_the_one_shot_body`) and
        with a plain run (`test_the_order_block_is_purely_additive`), and a
        field added to one side of an equality is a field that breaks it.
        LiteLLM puts its own call id on a header for the same reason -- an
        OpenAI-shaped response body is somebody else's schema.
        """
        payload = self._read_payload()
        if payload is None:
            return
        try:
            record = self._new_run("/generate")
        except (ValueError, _runs.RunIdInUse) as exc:
            self._send(400 if isinstance(exc, ValueError) else 409, {"error": str(exc)})
            return
        meter = _metering.current()
        try:
            hold = meter.begin(record.id)
        except _metering.InsufficientCredit as exc:
            record.finish("failed", detail=str(exc))
            status, body = _error_response(exc)
            self._send(status, body, run_id=record.id)
            return

        started = time.monotonic()

        def on_event(event: dict[str, Any]) -> None:
            # No frames go anywhere on this route, but the run still has to be
            # pollable and cancellable, and the callback is where both live.
            record.note(event)
            record.check()

        try:
            result = self._run(payload, on_event=on_event)
        except _runs.RunCancelled as exc:
            record.finish("cancelled", detail=str(exc))
            record.metering = meter.fail(
                hold, elapsed_s=time.monotonic() - started, reason="run cancelled"
            )
            status, body = _error_response(exc)
            self._send(status, body, run_id=record.id)
        except Exception as exc:
            record.finish("failed", detail=type(exc).__name__)
            record.metering = meter.fail(
                hold, elapsed_s=time.monotonic() - started, reason="run failed"
            )
            status, body = _error_response(exc)
            self._send(status, body, run_id=record.id)
        else:
            record.finish("done")
            record.metering = meter.finish(
                hold, elapsed_s=time.monotonic() - started
            )
            self._send(200, result, run_id=record.id)

    def _transcribe(self) -> None:
        """One spoken request in, its text out, through the same model seam.

        The transcript is whatever the model said, stripped -- silence and
        noise are the caller's to interpret, not this route's to veto. Errors
        follow the shared taxonomy: field problems are ValueErrors and answer
        as 400s, a failed model call answers as the 502 /generate would send.
        """
        payload = self._read_payload()
        if payload is None:
            return
        try:
            audio, mime_type, language, peak = transcribe_request(payload)
            if peak is not None and peak < TRANSCRIBE_PEAK_THRESHOLD:
                # Refused before the model call, and named: a window the
                # caller measured below the speech threshold carries no
                # speech, and the prompt's primed wake name is what silence
                # comes back as. Never a quiet 200 with an invented word.
                raise RequestError(
                    f"'peak' {peak:g} never reached the speech threshold "
                    f"{TRANSCRIBE_PEAK_THRESHOLD:g}: that window carried no "
                    "speech, so it was not transcribed"
                )
            if payload.get("quota_rpm") is not None:
                # Refused rather than ignored. 'quota_rpm' is the board-run
                # pace selector and every option it offers (3, 6, 15) is
                # slower than the voice floor, so honouring it here could only
                # ever make a spoken turn wait longer -- which is precisely the
                # bug this route had. Silently dropping a field the caller
                # named would hide that; a 400 says it.
                raise RequestError(
                    "'quota_rpm' is the board-run pace and does not apply to "
                    "/transcribe: voice paces on its own gate"
                )
            purpose = payload.get("purpose")
            if purpose is None:
                purpose = DEFAULT_TRANSCRIBE_PURPOSE
            if purpose not in TRANSCRIBE_PURPOSES:
                raise RequestError(
                    "'purpose' must be one of "
                    f"{', '.join(sorted(TRANSCRIBE_PURPOSES))}"
                )
            # Cheap single-provider factory, never the board failover ladder.
            # Every purpose paces on the voice gate: a board run cannot delay a
            # spoken turn and a spoken turn cannot delay a board run.
            model = self.transcribe_model_factory()
            self.voice_pacer.wait(VOICE_QUOTA_RPM)
            text = transcribe_audio(model, audio, mime_type, language=language)
        except Exception as exc:
            status, body = _error_response(exc)
            self._send(status, body)
            return
        self._send(
            200,
            {
                "text": text,
                # FallbackModel reports which tier actually answered; a plain
                # GeminiModel reports its configured id; a scripted stand-in
                # honestly reports neither.
                "model": getattr(model, "last_model", None)
                or getattr(model, "model", None),
            },
        )

    def _speak_report(self) -> None:
        """``GET /speak``: which voice would answer, and why not the others.

        Always 200, ``no-store``, the ``/integrations`` contract -- a client
        decides between the service's voice and its own ``speechSynthesis``
        from this, and a route that 500s when one engine is misconfigured
        would push it into guessing.
        """
        self._send(
            200, _tts.speak_report(self.tts_engines_factory()), cache_control="no-store"
        )

    def _speak(self) -> None:
        """``POST /speak``: one sentence in, audio frames out as they exist.

        No Content-Length and no chunked encoding, the ``/generate/stream``
        constraint for the same reason -- this server speaks HTTP/1.0 and a
        body is delimited by the connection closing. Every write is flushed,
        because a buffer that released the audio at the end would convert
        streaming synthesis back into the whole-file download this route was
        built to replace.

        The engine is resolved and its *first* frame pulled before any header
        is sent, so a failure is an HTTP status rather than a short body. Once
        bytes are on the wire there is no way left to say "that was a
        failure", and a truncated readback that a listener hears as Ada
        trailing off mid-net-name is precisely the quiet-zero this repo
        refuses everywhere else.
        """
        payload = self._read_payload()
        if payload is None:
            return
        try:
            try:
                request = _tts.speak_request(payload)
            except ValueError as exc:
                # Field problems only: re-raised in this module's own taxonomy
                # so they answer 400 with their text, while a TtsError below
                # keeps its own status.
                raise RequestError(str(exc)) from exc
            engines = self.tts_engines_factory()
            try:
                engine, frames = _tts.synthesize(engines, request)
            except ValueError as exc:
                # `resolve_engine` raises a plain ValueError for exactly one
                # thing: an engine name that does not exist. That is a caller
                # mistake and gets its text back.
                raise RequestError(str(exc)) from exc
        except Exception as exc:
            status, body = _speak_error(exc)
            self._send(status, body)
            return

        content_type = (
            "audio/wav" if request.audio_format == "wav" else "audio/L16"
        )
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        # Which voice actually answered, before a single frame is played. A
        # caller must be able to tell a local Kokoro from a metered hosted
        # call without listening to the audio to guess.
        self.send_header("X-Kaleo-Tts-Engine", engine.name)
        self.send_header("X-Kaleo-Sample-Rate", str(engine.sample_rate))
        self.end_headers()

        try:
            if request.audio_format == "wav":
                # Unknown-length header: the size is not knowable until the
                # last frame, and waiting for it would defeat the route.
                self.wfile.write(_tts.wav_header(engine.sample_rate))
                self.wfile.flush()
            for chunk in frames:
                self.wfile.write(chunk)
                self.wfile.flush()
        except ConnectionError:
            # The listener hung up -- barge-in, a closed tab. Stopping is the
            # correct response, and it is not an error worth a traceback.
            self.close_connection = True
        except _tts.TtsError as exc:
            # Mid-stream, after the 200. There is no status left to send, so
            # it goes to the log named rather than vanishing: the client hears
            # audio stop early and this is the only record of why.
            sys.stderr.write(f"/speak: {engine.name} failed mid-stream: {exc}\n")
            self.close_connection = True

    def _desk_resolve(self) -> None:
        """One desk snap in, a caption and optional SPA target out.

        The PNG is the point of the call. A field problem is a 400; a
        failed or unparseable model answer is a 502, the /transcribe
        taxonomy. This route never starts a board.
        """
        payload = self._read_payload(max_bytes=DESK_MAX_BODY_BYTES)
        if payload is None:
            return
        try:
            (
                png,
                utterance,
                cursor_x,
                cursor_y,
                width,
                height,
                candidates,
            ) = desk_request(payload)
            try:
                quota_rpm = select_quota_rpm(payload.get("quota_rpm"))
            except ValueError as exc:
                raise RequestError(str(exc)) from exc
            model = self.desk_model_factory()
            self.request_pacer.wait(
                DESK_QUOTA_RPM if quota_rpm is None else quota_rpm
            )
            result = resolve_desk(
                model,
                png,
                utterance,
                cursor_x=cursor_x,
                cursor_y=cursor_y,
                width=width,
                height=height,
                candidates=candidates,
            )
        except Exception as exc:
            status, body = _error_response(exc)
            self._send(status, body)
            return
        body = result.as_dict()
        body["model"] = getattr(model, "last_model", None) or getattr(
            model, "model", None
        )
        self._send(200, body)

    def _generate_stream(self) -> None:
        """The same run, reported while it happens, as NDJSON.

        No Content-Length and no chunked encoding: this server speaks HTTP/1.0,
        where a body is delimited by the connection closing. That is also why
        every frame is flushed the moment it is written -- a buffer that
        delivered the frames at the end would turn progress into a transcript.
        """
        payload = self._read_payload()
        if payload is None:
            return

        # Named before a single byte of the response is sent, so the id exists
        # before the work does -- and taken from the caller when it sent one.
        # LiteLLM's proxy reads the same header inbound and returns it outbound
        # (`litellm/proxy/common_request_processing.py`,
        # `ProxyBaseLLMRequestProcessing.common_processing_pre_call_logic`:
        # `request.headers.get("x-litellm-call-id", str(uuid.uuid4()))`), and
        # the value of that is that a client which chose the id can poll and
        # cancel a run whose response never reached it at all.
        try:
            record = self._new_run("/generate/stream")
        except (ValueError, _runs.RunIdInUse) as exc:
            self._send(400 if isinstance(exc, ValueError) else 409, {"error": str(exc)})
            return
        meter = _metering.current()

        # Reserve before the 200. A refusal here has cost nothing -- no model
        # call, no solve -- and it is the only place a refusal is honest.
        try:
            hold = meter.begin(record.id)
        except _metering.InsufficientCredit as exc:
            record.finish("failed", detail=str(exc))
            status, body = _error_response(exc)
            self._send(status, body, run_id=record.id)
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        # The header, not only the frame. A body that never parses -- a proxy
        # that rewrote the type, a transport with no reader -- is exactly the
        # case `api.js::startedButUnreadable` reports, and until now that
        # client had no way to name the run it had just paid for. LiteLLM
        # returns its own id the same way, as `x-litellm-call-id`
        # (`litellm/proxy/proxy_server.py::get_custom_headers`).
        self.send_header("X-Kaleo-Run-Id", record.id)
        self.end_headers()

        gone = False
        started = time.monotonic()

        def emit(event: dict[str, Any]) -> None:
            """One frame, on the wire immediately.

            A write to a client that has hung up re-raises on purpose: the
            pipeline lets a callback's exception abandon the run, which is how
            a reader's disconnect cancels the work being done for it. The
            cancel route rides that same mechanism rather than adding a second
            one -- `record.check` raises `RunCancelled` from right here.
            """
            nonlocal gone
            record.note(event)
            record.check()
            try:
                self.wfile.write(_json_line(event))
                self.wfile.flush()
            except ConnectionError:
                # BrokenPipeError and ConnectionResetError are the two that
                # normally arrive; Windows reports the same disconnect as
                # ConnectionAbortedError, and all three are ConnectionError.
                gone = True
                raise

        def elapsed() -> float:
            return time.monotonic() - started

        try:
            # Before any work starts, so a client can tell an accepted request
            # from one still waiting for a connection -- and so the run id is
            # in the body as well as the header.
            emit({"event": "run.accepted", "t_s": 0.0, "run_id": record.id})
            result = self._run(payload, on_event=emit)
        except _runs.RunCancelled as exc:
            record.finish("cancelled", detail=str(exc))
            # Charged for what it used, not refunded: the model calls are
            # already spent, so a late cancel must not be cheaper than
            # finishing (billing/ledger.py::MemoryLedger.release).
            record.metering = meter.fail(
                hold, elapsed_s=elapsed(), reason="run cancelled"
            )
            with contextlib.suppress(ConnectionError):
                emit(
                    {
                        "event": "run.cancelled",
                        "run_id": record.id,
                        "detail": str(exc),
                        "metering": record.metering,
                    }
                )
            return
        except Exception as exc:
            record.finish("failed", detail=type(exc).__name__)
            record.metering = meter.fail(hold, elapsed_s=elapsed(), reason="run failed")
            if gone:
                # The exception is our own emit reporting the disconnect, on
                # its way out through the pipeline. There is nobody left to
                # tell, and writing again would only raise a second time. The
                # run stays addressable, which is the whole point: the client
                # that lost this stream can still ask what it cost.
                sys.stderr.write(
                    f"stream client disconnected: {self.path} ({record.id})\n"
                )
                return
            status, body = _error_response(exc)
            # Whether this last frame lands is the client's business now; if
            # it left between the failure and this write there is nothing
            # further to do about it.
            with contextlib.suppress(ConnectionError):
                emit(
                    {
                        "event": "run.error",
                        "status": status,
                        "run_id": record.id,
                        **body,
                    }
                )
            return

        record.finish("done")
        record.metering = meter.finish(hold, elapsed_s=elapsed())
        try:
            emit(
                {
                    "event": "run.done",
                    "run_id": record.id,
                    "metering": record.metering,
                    "result": result,
                }
            )
        except _ResponseSerializationError as exc:
            status, body = _error_response(exc)
            with contextlib.suppress(ConnectionError):
                emit(
                    {
                        "event": "run.error",
                        "status": status,
                        "run_id": record.id,
                        **body,
                    }
                )
        except (ConnectionError, _runs.RunCancelled):
            # A cancel that lands on the very last frame stopped nothing; the
            # run is already committed and its state already says `done`.
            pass

    def _chat_stream(self) -> None:
        """One ADK orchestrator turn, with pipeline and model events inline."""
        payload = self._read_payload()
        if payload is None:
            return

        intent = payload.get("intent")
        clarification = payload.get("clarification", "")
        if not isinstance(intent, str) or not intent.strip():
            self._send(400, {"error": "'intent' is required"})
            return
        if not isinstance(clarification, str):
            self._send(400, {"error": "'clarification' must be a string"})
            return

        session_id = str(payload.get("session_id") or uuid.uuid4().hex)
        turn_id = str(payload.get("turn_id") or uuid.uuid4().hex[:12])
        if len(session_id) > 128 or len(turn_id) > 128:
            self._send(
                400,
                {"error": "session and turn ids must be at most 128 characters"},
            )
            return

        try:
            # Validate board constraints before even resolving the chat model.
            # generate() repeats this at the tool boundary so direct calls get
            # the same guarantee.
            parse_constraint_manifest(payload.get("constraints"))
            catalog = self.model_catalog_factory()
            orchestrator_model = select_model(payload.get("model"), catalog)
            thinking_level = select_thinking_level(payload.get("thinking_level"))
            quota_rpm = select_quota_rpm(payload.get("quota_rpm"))
        except ValueError as exc:
            self._send(400, {"error": str(exc)})
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        started = time.monotonic()
        event_seq = 0
        gone = False

        def emit(event: dict[str, Any]) -> None:
            nonlocal event_seq, gone
            event_seq += 1
            frame = {
                "schema_version": 1,
                "session_id": session_id,
                "turn_id": turn_id,
                "event_id": f"e{event_seq}",
                **event,
            }
            frame.setdefault("t_s", round(time.monotonic() - started, 3))
            try:
                self.wfile.write(_json_line(frame))
                self.wfile.flush()
            except ConnectionError:
                gone = True
                raise

        def generate_board() -> dict[str, Any]:
            board_payload = {
                key: value
                for key, value in payload.items()
                if key
                not in {
                    "clarification",
                    "session_id",
                    "turn_id",
                    "model",
                    "thinking_level",
                    "quota_rpm",
                }
            }
            if clarification.strip():
                board_payload["intent"] = (
                    f"{intent.strip()}\n\nClarification: {clarification.strip()}"
                )
            return self._run(board_payload, on_event=emit, quota_rpm=quota_rpm)

        def pace_orchestrator() -> None:
            self.request_pacer.wait(
                quota_rpm,
                on_wait=lambda delay: emit(
                    {
                        "event": "quota.wait",
                        "layer": "orchestrator",
                        "model": orchestrator_model,
                        "quota_rpm": quota_rpm,
                        "delay_s": round(delay, 3),
                    }
                ),
            )

        try:
            emit(
                {
                    "event": "chat.accepted",
                    "layer": "orchestrator",
                    "model": orchestrator_model,
                    "thinking_level": thinking_level or "auto",
                    "quota_rpm": quota_rpm or "auto",
                }
            )
            outcome = self.orchestrator_runner(
                message=intent,
                clarification=clarification,
                model=orchestrator_model,
                thinking_level=thinking_level,
                session_id=session_id,
                generate=generate_board,
                emit=emit,
                debug=bool(payload.get("debug", False)),
                before_model_call=pace_orchestrator,
            )
        except Exception as exc:
            if gone:
                sys.stderr.write(f"stream client disconnected: {self.path}\n")
                return
            status, body = _error_response(exc)
            with contextlib.suppress(ConnectionError):
                emit({"event": "chat.error", "status": status, **body})
            return

        try:
            emit(
                {
                    "event": "chat.done",
                    "assistant": outcome.assistant,
                    "needs_clarification": outcome.needs_clarification,
                    "model": outcome.model,
                    "thinking_level": thinking_level or "auto",
                    "quota_rpm": quota_rpm or "auto",
                    "result": outcome.result,
                }
            )
        except _ResponseSerializationError as exc:
            status, body = _error_response(exc)
            with contextlib.suppress(ConnectionError):
                emit({"event": "chat.error", "status": status, **body})
        except ConnectionError:
            pass

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"{self.address_string()} - {fmt % args}\n")


def make_server(port: int | None = None) -> ThreadingHTTPServer:
    port = port if port is not None else int(os.getenv("PORT", "8080"))
    return ThreadingHTTPServer(("0.0.0.0", port), Handler)


def main() -> int:  # pragma: no cover - process entry
    # What the Setup Assistant saved under ~/.kaleo, applied setdefault so a
    # value already exported still wins. Here, not at import: a test that
    # imports this module must never read the developer's real files.
    _envfiles.apply_saved_env()
    server = make_server()
    sys.stderr.write(f"listening on :{server.server_port}\n")
    server.serve_forever()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

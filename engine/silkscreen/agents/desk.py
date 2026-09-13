"""Resolve a spoken deictic against a desk screenshot.

The desktop overlay captures a PNG with the cursor burned in and the
engineer's words ("what's this", "I don't like this"). This module is the
only place that screenshot is shown to a model. The answer is a caption
the overlay can speak, an abstain flag when the pointer is unreadable, and
an optional SPA target the in-app guide can point at. It never invents a
board intent and never writes KiCad.

The PNG travels as a :class:`~silkscreen.agents.model.Document` on the
existing :class:`Model` protocol -- the same seam
:func:`~silkscreen.agents.transcribe.transcribe_audio` uses for audio --
so :class:`ScriptedModel` keeps the tests offline. Intended live tier:
:data:`~silkscreen.agents.model.CHEAP_MODEL`. The tier is the caller's
to construct; this module only ever sees the protocol.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .model import Document, Model, ModelError, parse_json

__all__ = [
    "DESK_MARKER",
    "DESK_PROMPT",
    "DeskCandidate",
    "DeskResolution",
    "DeskTarget",
    "DeskValidationError",
    "parse_desk_response",
    "resolve_desk",
]

#: Frozen: appears verbatim in every prompt so a ``ScriptedModel.by_marker``
#: can key on it, the ``SOURCING_MARKER`` convention.
DESK_MARKER = "DESK-RESOLVE v1"

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

#: Spoken answers stay short enough to read on the overlay and to speak.
MAX_CAPTION_CHARS = 400

#: SPA testids the guide already knows how to resolve. The model may name
#: one of these even when the client sent no live candidates; a target the
#: document does not contain degrades to caption-only, which is honest.
_KNOWN_TESTIDS = (
    "finding-card",
    "finding-card-show-board",
    "board-well-part",
    "schematic-part",
    "sourcing-row",
)


class DeskValidationError(ValueError):
    """The model's desk answer is not usable.

    ``errors`` holds one message per problem so the whole batch can go back
    to a model in a single repair prompt (the :mod:`silkscreen.netlist`
    convention). This MVP has no repair loop; the service surfaces the
    batch as a failed resolve rather than inventing a target.
    """

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__(
            f"{len(errors)} problem(s) in desk response:\n  - "
            + "\n  - ".join(errors)
        )


@dataclass(frozen=True)
class DeskCandidate:
    """One on-screen control the client can still find by testid + attrs."""

    testid: str
    attrs: dict[str, str] = field(default_factory=dict)
    tab: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"testid": self.testid, "attrs": dict(self.attrs)}
        if self.tab:
            out["tab"] = self.tab
        return out


@dataclass(frozen=True)
class DeskTarget:
    """Where the in-app guide should point, or nothing.

    ``testid`` plus identity ``attrs`` (``ref``, ``sev``, …) -- never an
    index -- so a reordered list cannot silently rebind the pointer.
    """

    testid: str
    attrs: dict[str, str] = field(default_factory=dict)
    tab: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"testid": self.testid, "attrs": dict(self.attrs)}
        if self.tab:
            out["tab"] = self.tab
        return out


@dataclass(frozen=True)
class DeskResolution:
    """What :func:`resolve_desk` accepted."""

    caption: str
    abstain: bool
    target: DeskTarget | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "caption": self.caption,
            "abstain": self.abstain,
            "target": None if self.target is None else self.target.as_dict(),
        }


DESK_PROMPT = f"""\
You are {DESK_MARKER}: resolving a spoken deictic against a desk screenshot.
The attached PNG is one captured desktop frame. The cursor is burned into
the image when the OS allowed it; the prompt also names the cursor pixel
so you can find the pointer even if the burn-in is faint. The engineer
said a short phrase that means "what I am pointing at", not a request to
design a new board.

Respond with ONE JSON object -- no prose, no code fence:

{{
  "caption": "<one or two spoken sentences naming what is under the pointer>",
  "abstain": true | false,
  "target": null | {{
    "testid": "<data-testid of the control>",
    "attrs": {{"<identity attr>": "<value>"}},
    "tab": "<optional SPA tab: review|board|schematic|sourcing|case>"
  }}
}}

Rules:
- caption is what Ada says out loud. Be specific (the finding title, the
  part ref, the control label). Bound it to a couple of sentences.
- abstain=true when you cannot see what the pointer is on, the screenshot
  is blank or occluded, or the utterance is not about the screen. Then
  target MUST be null. Still write a short caption that says so honestly.
- target is an in-app control, not a KiCad or OS coordinate. Prefer a
  candidate the prompt listed. You may also name one of these well-known
  SPA testids when the screenshot clearly shows that control:
  finding-card (attrs: sev, selected, parts), finding-card-show-board,
  board-well-part (attrs: ref), schematic-part (attrs: ref),
  sourcing-row (attrs: ref).
- Never invent a testid, a ref, or a severity the screenshot does not
  support. A null target plus an honest caption is better than a guess.
- Do not propose a circuit, a part number, or a new board. This call is
  not /generate.
"""


def _clean_attrs(raw: object, *, where: str, errors: list[str]) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        errors.append(f'{where}: "attrs" must be an object of string values')
        return {}
    attrs: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip():
            errors.append(f"{where}: attrs keys must be non-empty strings")
            continue
        if not isinstance(value, str):
            errors.append(
                f"{where}: attrs[{key!r}] must be a string, "
                f"got {type(value).__name__}"
            )
            continue
        attrs[key.strip()] = value
    return attrs


def _parse_target(
    raw: object, *, errors: list[str]
) -> DeskTarget | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        errors.append('"target" must be null or an object')
        return None
    testid = raw.get("testid")
    if not isinstance(testid, str) or not testid.strip():
        errors.append('"target.testid" must be a non-empty string')
        testid = None
    # Keep collecting attrs/tab errors even when testid is missing so one
    # repair prompt can name every problem (the netlist convention).
    attrs = _clean_attrs(raw.get("attrs"), where="target", errors=errors)
    tab = raw.get("tab")
    if tab is not None and not isinstance(tab, str):
        errors.append('"target.tab" must be a string or null')
        tab = None
    tab = (tab or "").strip() or None
    if testid is None:
        return None
    return DeskTarget(testid=testid.strip(), attrs=attrs, tab=tab)


def _candidate_accepts(target: DeskTarget, candidate: DeskCandidate) -> bool:
    if target.testid != candidate.testid:
        return False
    for key, value in target.attrs.items():
        if candidate.attrs.get(key) != value:
            return False
    return not (target.tab and candidate.tab and target.tab != candidate.tab)


def parse_desk_response(
    raw: str,
    *,
    candidates: Sequence[DeskCandidate] | None = None,
) -> DeskResolution:
    """Validate one model answer into a :class:`DeskResolution`.

    Every failure is collected and raised together as
    :class:`DeskValidationError`, unparseable JSON included, so one
    repair round could fix all of them. A target that names no listed
    candidate (when the client sent a list) is a failure, not a silent
    drop -- dropping it would report a caption that points at nothing
    while claiming a target.
    """
    errors: list[str] = []
    try:
        data = parse_json(raw)
    except ModelError as exc:
        raise DeskValidationError([f"response was not JSON: {exc}"]) from exc

    if not isinstance(data, dict):
        raise DeskValidationError(
            [f"top level must be a JSON object, got {type(data).__name__}"]
        )

    caption_raw = data.get("caption")
    if not isinstance(caption_raw, str):
        errors.append('"caption" must be a string')
        caption = ""
    else:
        caption = caption_raw.strip()[:MAX_CAPTION_CHARS]

    abstain_raw = data.get("abstain")
    if not isinstance(abstain_raw, bool):
        errors.append('"abstain" must be a boolean')
        abstain = True
    else:
        abstain = abstain_raw

    target = _parse_target(data.get("target"), errors=errors)
    if abstain and target is not None:
        errors.append("abstain is true, so target must be null")
        target = None
    if not abstain and not caption:
        errors.append('"caption" is required when abstain is false')

    listed = list(candidates or ())
    if (
        target is not None
        and listed
        and not any(_candidate_accepts(target, item) for item in listed)
    ):
        errors.append(
            f"target {target.testid!r} does not match any listed candidate"
        )

    if errors:
        raise DeskValidationError(errors)
    return DeskResolution(caption=caption, abstain=abstain, target=target)


def _candidates_block(candidates: Sequence[DeskCandidate]) -> str:
    if not candidates:
        return (
            "No live candidates were supplied. You may still name a "
            f"well-known SPA testid ({', '.join(_KNOWN_TESTIDS)}) when the "
            "screenshot clearly shows that control, or set target to null."
        )
    lines = ["Live candidates the client can still resolve:"]
    for item in candidates:
        lines.append(f"  - {item.as_dict()}")
    lines.append(
        "If you set a target it must match one of these (testid, and any "
        "attrs you name must equal that candidate's)."
    )
    return "\n".join(lines)


def resolve_desk(
    model: Model,
    png: bytes,
    utterance: str,
    *,
    cursor_x: float,
    cursor_y: float,
    width: int | None = None,
    height: int | None = None,
    candidates: Sequence[DeskCandidate] | None = None,
) -> DeskResolution:
    """One PNG + spoken deixis in, a caption and optional target out.

    ``png`` must be a real PNG -- the bytes travel as an inline image
    ``Document``, never a URL. A caller that already validated the upload
    still hits this check so a unit test cannot slip a text part through
    and call it a screenshot.
    """
    if not png or not png.startswith(_PNG_MAGIC):
        raise ValueError("png must be a PNG image")
    text = utterance.strip()
    if not text:
        raise ValueError("utterance is required")

    listed = tuple(candidates or ())
    size = ""
    if width and height:
        size = f" The image is {int(width)}×{int(height)} pixels."
    prompt = (
        f"{DESK_PROMPT}\n"
        f"Utterance: {text}\n"
        f"Cursor (image pixels, origin top-left): "
        f"{cursor_x:.1f}, {cursor_y:.1f}.{size}\n"
        f"{_candidates_block(listed)}\n"
    )
    raw = model.generate(
        prompt,
        documents=[Document(data=png, mime_type="image/png")],
        temperature=0.0,
        max_output_tokens=1024,
    )
    return parse_desk_response(raw, candidates=listed)

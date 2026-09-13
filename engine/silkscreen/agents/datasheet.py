"""Read a datasheet into structured facts.

Gemini's native PDF vision is what makes this tractable. A datasheet's pinout
table and package drawing are *pictures*; text extraction throws away exactly
the information needed here, which is why the previous project's PyPDF2
dependency was never actually used and its one model call passed a bare URL.

Every fact carries the page it came from. A claim without a citation is not
useful in a domain where being wrong costs four weeks and a fab run.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields

from .model import Document, Model, ModelError, parse_json

__all__ = [
    "PartFacts",
    "PinFact",
    "read_datasheet",
    "DATASHEET_PROMPT",
    "MAX_INLINE_PDF_BYTES",
]

#: The most PDF we will put in one request. Gemini's ceiling is a 20MB *total
#: request* and inline bytes travel base64, which inflates by 4/3, so 15MB of
#: PDF is about all that fits. ``fetch_pdf`` defaults to 50MB, which would sail
#: past it and fail at the API instead of here, naming nothing useful.
MAX_INLINE_PDF_BYTES = 15_000_000


@dataclass(frozen=True)
class PinFact:
    number: str
    name: str
    kind: str = ""          # power / ground / input / output / bidirectional / analog
    description: str = ""
    page: int | None = None


@dataclass
class PartFacts:
    """What we learned about one component."""

    part_number: str
    package: str = ""
    pin_count: int = 0
    pins: list[PinFact] = field(default_factory=list)
    #: Free-text requirements the reviewer will check against, each with a page.
    requirements: list[dict] = field(default_factory=list)
    #: Recommended auxiliary components, as {name, type, value, why, page}.
    auxiliaries: list[dict] = field(default_factory=list)
    notes: str = ""
    source_url: str = ""

    def to_dict(self) -> dict:
        """A JSON-safe copy, for storing in a cache.

        Reading a datasheet is the slowest and most expensive stage in the
        pipeline and its result is a pure function of the part number, so the
        facts are worth persisting. Persisting them requires that they survive
        a round trip *whole* -- a cache that stores only a part number lets the
        caller skip the read and then design with nothing.
        """
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> PartFacts:
        """Rebuild from :meth:`to_dict`, ignoring fields we do not know.

        Unknown keys are dropped rather than raising: a cache entry written by
        a newer version of this class must not break an older instance still
        serving traffic, which on Cloud Run is a normal state during a rollout.

        Malformed input raises :class:`ValueError` -- one type, so a caller can
        treat a corrupt entry as a miss without having to enumerate every way a
        dict can be wrong.
        """
        if not isinstance(data, dict):
            raise ValueError(f"expected a dict of facts, got {type(data).__name__}")

        known = {f.name for f in fields(cls)} - {"pins"}
        kwargs = {k: v for k, v in data.items() if k in known}

        raw_pins = data.get("pins") or ()
        if not isinstance(raw_pins, (list, tuple)):
            raise ValueError(f"'pins' must be a list, got {type(raw_pins).__name__}")

        pin_fields = {f.name for f in fields(PinFact)}
        pins = []
        for pin in raw_pins:
            if not isinstance(pin, dict):
                raise ValueError(
                    f"each pin must be an object, got {type(pin).__name__}"
                )
            pins.append(PinFact(**{k: v for k, v in pin.items() if k in pin_fields}))
        kwargs["pins"] = pins

        try:
            return cls(**kwargs)
        except TypeError as exc:  # a known key carrying the wrong shape
            raise ValueError(f"malformed facts: {exc}") from exc

    def pin_map(self) -> dict[str, str]:
        """``{pin_name: pin_number}`` in the shape :mod:`silkscreen.netlist` wants."""
        return {p.name: p.number for p in self.pins}

    def pin_by_name(self, name: str) -> PinFact | None:
        for pin in self.pins:
            if pin.name.lower() == name.lower():
                return pin
        return None


DATASHEET_PROMPT = """\
You are reading an electronic component datasheet. Extract only what the
document actually states. Do not infer, do not fill gaps from general
knowledge, and do not guess a pin number you cannot see.

Return ONE JSON object, no prose, no code fence:

{
  "part_number": "<exact orderable part number>",
  "package": "<e.g. LQFP-48, SOT-223-3, SOIC-8>",
  "pin_count": <integer, the package pin count>,
  "pins": [
    {"number": "<physical pin number>", "name": "<datasheet pin name>",
     "kind": "power|ground|input|output|bidirectional|analog|nc",
     "description": "<short>", "page": <page number you read this from>}
  ],
  "requirements": [
    {"requirement": "<one electrical requirement for correct operation>",
     "page": <page number>}
  ],
  "auxiliaries": [
    {"name": "<descriptive id, e.g. c_dec_vdd>",
     "type": "capacitor|resistor|inductor|diode|crystal",
     "value": "<e.g. 100nF>",
     "connects": "<which pins it goes between, using datasheet pin names>",
     "why": "<short reason>", "page": <page number>}
  ],
  "notes": "<anything a designer would be caught out by>"
}

Rules:
- "pins" must list EVERY pin of the package, numbered as the package numbers them.
- Auxiliary types are limited to the five listed. If the datasheet requires
  something outside that set, put it in "notes" instead of inventing a type.
- Every requirement and auxiliary MUST carry the page it came from.
- If the document does not state something, omit it. An empty list is a valid
  and useful answer; a fabricated one is not.
"""


def _download(url: str, fetch: Callable[..., bytes] | None) -> bytes:
    """Fetch a datasheet PDF and prove that is what arrived.

    A 200 response is not evidence of a PDF. Distributors increasingly serve an
    HTML viewer page from a ``.pdf`` URL -- LCSC's AMS1117 link does exactly
    that -- and handing those bytes to the model as ``application/pdf`` buys a
    400 from the API naming neither the part nor the URL. Checking the magic
    here is what turns that into a sentence someone can act on.

    ``grounding`` is imported inside the function on purpose: it imports
    ``review``, which imports this module for ``PartFacts``, so importing it at
    module scope would close that cycle.
    """
    from .grounding import GroundingError, fetch_pdf

    data = (fetch or fetch_pdf)(url, max_bytes=MAX_INLINE_PDF_BYTES)
    if not data.startswith(b"%PDF-"):
        raise GroundingError(
            f"{url} did not return a PDF: the body begins {data[:16]!r}. "
            f"A distributor link that renders a viewer page is the usual "
            f"cause; use the manufacturer's own PDF."
        )
    return data


def read_datasheet(
    model: Model,
    part_number: str,
    *,
    pdf_url: str | None = None,
    pdf_bytes: bytes | None = None,
    fetch: Callable[..., bytes] | None = None,
) -> PartFacts:
    """Extract structured facts for ``part_number`` from its datasheet.

    A ``pdf_url`` is **downloaded here** and sent as bytes. Gemini does not
    fetch arbitrary URLs: its ``file_uri`` field accepts a Files API URI or a
    YouTube link and nothing else, and handing it a public datasheet URL returns
    a bare ``429 RESOURCE_EXHAUSTED`` -- no quota metric, no retry delay -- which
    reads as an exhausted key and survives every failover attempt, because
    retrying cannot fix a malformed request.

    ``fetch`` exists so the suite stays offline, the same reason ``Model`` has
    ``ScriptedModel`` behind it. It defaults to :func:`grounding.fetch_pdf`,
    which validates the URL against SSRF, caps redirects, and caps the body.
    """
    if not pdf_url and not pdf_bytes:
        raise ValueError("read_datasheet needs a pdf_url or pdf_bytes")

    if pdf_bytes is None:
        pdf_bytes = _download(pdf_url, fetch)

    # Bytes, never the url: see this function's docstring for what passing a
    # public URL through to the provider actually does.
    doc = Document(data=pdf_bytes)
    prompt = f"{DATASHEET_PROMPT}\n\nThe part is: {part_number}"
    raw = model.generate(prompt, documents=[doc], temperature=0.0)
    data = parse_json(raw)

    if not isinstance(data, dict):
        raise ModelError(f"Expected a JSON object, got {type(data).__name__}")

    pins = []
    for entry in data.get("pins") or []:
        if not isinstance(entry, dict):
            continue
        number = str(entry.get("number", "")).strip()
        name = str(entry.get("name", "")).strip()
        if not number or not name:
            continue
        pins.append(
            PinFact(
                number=number,
                name=name,
                kind=str(entry.get("kind", "")),
                description=str(entry.get("description", "")),
                page=entry.get("page") if isinstance(entry.get("page"), int) else None,
            )
        )

    if not pins:
        raise ModelError(
            f"No pins extracted for {part_number}. The pipeline cannot place a "
            f"part whose pinout is unknown."
        )

    facts = PartFacts(
        part_number=str(data.get("part_number") or part_number),
        package=str(data.get("package", "")),
        pin_count=int(data.get("pin_count") or 0) or len(pins),
        pins=pins,
        requirements=[
            r for r in (data.get("requirements") or []) if isinstance(r, dict)
        ],
        auxiliaries=[a for a in (data.get("auxiliaries") or []) if isinstance(a, dict)],
        notes=str(data.get("notes", "")),
        source_url=pdf_url or "",
    )

    # A pin count that disagrees with the pin list means one of them is wrong,
    # and picking a package off a wrong count produces a dead board.
    if facts.pin_count and len(facts.pins) != facts.pin_count:
        facts.notes += (
            f" [silkscreen: datasheet reports {facts.pin_count} pins but "
            f"{len(facts.pins)} were extracted; package choice may be wrong]"
        )
    return facts

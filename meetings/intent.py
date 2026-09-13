"""Find the board requests hiding in a meeting transcript.

A standup is not a form. Somewhere in forty minutes of scheduling and
apologies, someone says "we need a little 3.3V regulator board for the sensor
rig" -- and that sentence is a complete pipeline input. This module asks a
model to point at those sentences and nothing else.

**The model is a witness, not an author.** Everything it returns is treated as
a claim about the transcript, and a claim that the transcript does not support
is thrown away. The failure this guards against is specific and expensive: a
model asked to summarise a meeting will happily produce a plausible hardware
requirement nobody stated, and downstream there is no human in the loop -- the
next thing that happens is a paid pipeline run, a schematic, and a board for a
project that does not exist. So every request must carry the verbatim line it
came from, and a quote that is not in the transcript takes its request with it.

**Truncation is safe here, and only here.** ``max_requests`` caps how many
requests one meeting can produce, keeping the highest-confidence ones. That is
not data loss: the transcript is the durable artefact and stays exactly where
it was, the cap exists because each surviving request becomes an independent
paid run, and what gets dropped is what the model itself was least sure of.
The alternative -- one rambling meeting spawning eleven board runs -- is the
bug this prevents.

**Nothing here returns a quiet zero.** An empty list means the model read the
meeting and found no hardware request in it. Every other outcome -- unparseable
output, a missing field, a confidence outside 0..1, a model that invented every
quote it gave -- raises. An agent handed an empty result concludes there was
nothing to do, so "nothing to do" has to be a fact rather than a shrug.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass

from silkscreen.agents.model import Model, strip_code_fence

__all__ = [
    "BoardRequest",
    "IntentError",
    "extract_requests",
    "EXTRACT_PROMPT",
    "MAX_TRANSCRIPT_CHARS",
]

#: A transcript longer than this is truncated before it reaches the model.
#: Meet returns whole conferences, and a three-hour all-hands would otherwise
#: turn one poll into an enormous prompt. The tail is kept rather than the
#: head: requirements get stated after the agenda, not before it.
MAX_TRANSCRIPT_CHARS = 60_000

#: A quote longer than this is refused. A "verbatim line" that runs to a page
#: is a model pasting the transcript back, which defeats the whole point of
#: the containment check -- a large enough quote matches by accident.
MAX_QUOTE_CHARS = 600

#: Same idea for the other end: a two-word quote ("the board") appears in
#: almost any transcript, so containment proves nothing about it.
MIN_QUOTE_CHARS = 12


EXTRACT_PROMPT = """\
You are reading the transcript of an engineering meeting. Find every request \
for a PRINTED CIRCUIT BOARD or hardware design that someone actually stated in \
it.

Rules, in order of importance:
1. Only report a request that someone SAID. Do not infer one from context, do \
not combine two speakers into one request, and do not report something the \
meeting merely implies would be useful.
2. Every request must carry the exact transcript text it came from, copied \
character for character into "quote". If you cannot copy a line that states \
the request, the request is not there -- leave it out.
3. Discussion of an existing board, a complaint about hardware, or a question \
about a part is NOT a request. A request asks for something to be designed or \
built.
4. If the meeting contains no such request, return {"requests": []}. That is a \
correct and expected answer, not a failure.

Return JSON only, in exactly this shape:

{
  "requests": [
    {
      "intent": "a single self-contained sentence describing the board, \
written so an engineer who did not attend could act on it",
      "quote": "the verbatim transcript line(s) stating the request",
      "speaker": "the speaker label from the transcript line",
      "confidence": 0.0 to 1.0
    }
  ]
}

TRANSCRIPT:
{transcript}
"""


class IntentError(ValueError):
    """The model's answer could not be trusted.

    ``errors`` holds one message per problem, collected rather than raised on
    the first, so a repair prompt can address the whole batch at once -- the
    same contract :class:`silkscreen.netlist.ValidationError` keeps, for the
    same reason: one round trip per broken response instead of one per field.
    """

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__(
            f"{len(self.errors)} problem(s) in extracted board requests:\n  - "
            + "\n  - ".join(self.errors)
        )


@dataclass(frozen=True)
class BoardRequest:
    """One board someone asked for, and the evidence that they asked.

    ``quote`` is not decoration. It is the only thing that distinguishes a
    requirement from a hallucination, it is what a human sees when they are
    asked to approve the run, and it is why this dataclass cannot be
    constructed usefully without one.
    """

    intent: str
    quote: str
    confidence: float
    speaker: str = ""

    def __str__(self) -> str:
        who = self.speaker or "someone"
        return f"{who} ({self.confidence:.2f}): {self.intent}"


def _normalise(text: str) -> str:
    """Collapse a transcript or a quote to one comparable form.

    Whitespace is the only thing allowed to differ. A model re-wraps lines,
    joins two utterances with a space, or drops the newline between them, and
    an exact ``in`` test would reject a perfectly honest quote for it. Case is
    folded for the same reason. Nothing else is normalised: if the words are
    not the transcript's words, the quote is not a quote.
    """
    return re.sub(r"\s+", " ", text).strip().casefold()


def _speaker_of(transcript_lines: list[str], quote: str) -> str:
    """Whose line did this quote come from?

    Used only when the model omitted the speaker. Meet's labels are opaque
    participant ids, so a model has no way to invent a correct one -- taking it
    from the line we already matched is strictly more reliable than asking.
    """
    needle = _normalise(quote)
    for line in transcript_lines:
        speaker, sep, _ = line.partition(":")
        # Match against the whole line, not the body: a model quotes a line
        # with its "carol:" prefix about as often as without, and a check
        # written against the body alone silently fails on half of them.
        if sep and needle in _normalise(line):
            return speaker.strip()
    return ""


def _coerce_confidence(value: object, where: str, errors: list[str]) -> float | None:
    """Validate a confidence, or record why it is not one.

    A missing or unreadable confidence is an error rather than a default,
    because every plausible default is wrong in a way that hurts: 0.0 hides a
    real request behind the ranking, and 1.0 promotes a guess to a fact and
    spends money on it.
    """
    # ``bool`` is an ``int`` in Python, and ``True`` would otherwise sail
    # through as a perfect 1.0 confidence.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(
            f"{where}: 'confidence' must be a number between 0 and 1, got "
            f"{value!r}"
        )
        return None
    number = float(value)
    if not math.isfinite(number):
        errors.append(f"{where}: 'confidence' is not a finite number ({value!r})")
        return None
    if not 0.0 <= number <= 1.0:
        errors.append(
            f"{where}: 'confidence' must be between 0 and 1, got {number}"
        )
        return None
    return number


def _text_field(item: dict, key: str, where: str, errors: list[str]) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{where}: '{key}' must be a non-empty string, got {value!r}")
        return ""
    return value.strip()


def extract_requests(
    model: Model,
    transcript_text: str,
    *,
    max_requests: int = 3,
) -> list[BoardRequest]:
    """Ask ``model`` which board requests this meeting actually contains.

    Returns at most ``max_requests`` requests, highest confidence first, every
    one of which quotes a line that is genuinely in ``transcript_text``.

    Raises :class:`IntentError` when the model's answer cannot be validated,
    and lets :class:`silkscreen.agents.model.ModelError` through untouched --
    a call that failed is not a meeting with no requests in it.
    """
    if max_requests < 1:
        raise ValueError(f"max_requests must be at least 1, got {max_requests}")
    if not isinstance(transcript_text, str):
        raise TypeError(
            f"transcript_text must be a string of 'speaker: text' lines, got "
            f"{type(transcript_text).__name__}"
        )

    transcript = transcript_text.strip()
    if not transcript:
        # No call, and no error. There is nothing here to misread, and a paid
        # request that asks a model to find requests in an empty document is
        # an invitation to invent one.
        return []
    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        transcript = transcript[-MAX_TRANSCRIPT_CHARS:]

    raw = model.generate(
        EXTRACT_PROMPT.replace("{transcript}", transcript),
        system=(
            "You extract stated requirements from meeting transcripts. You "
            "quote; you do not paraphrase and you do not infer."
        ),
        temperature=0.0,
    )
    return parse_requests(raw, transcript, max_requests=max_requests)


def parse_requests(
    raw: str | dict,
    transcript_text: str,
    *,
    max_requests: int = 3,
) -> list[BoardRequest]:
    """Validate one model answer against the transcript it claims to describe.

    Separate from :func:`extract_requests` so the untrusted half can be tested
    without a model at all, and so a caller holding a recorded response can
    re-check it later.
    """
    if isinstance(raw, str):
        try:
            data = json.loads(strip_code_fence(raw))
        except json.JSONDecodeError as exc:
            # The first 200 characters are in the message on purpose: "not
            # valid JSON" alone sends a person to the logs to find out whether
            # the model returned prose, an apology, or half a document.
            cleaned = strip_code_fence(raw)
            raise IntentError(
                [f"response is not valid JSON: {exc}; first 200 chars: "
                 f"{cleaned[:200]!r}"]
            ) from exc
    else:
        data = raw

    if not isinstance(data, dict):
        raise IntentError(
            [
                f"expected a JSON object with a 'requests' array, got "
                f"{type(data).__name__}"
            ]
        )
    items = data.get("requests")
    if items is None:
        raise IntentError(["response has no 'requests' key"])
    if not isinstance(items, list):
        raise IntentError(
            [f"'requests' must be an array, got {type(items).__name__}"]
        )

    transcript_lines = [
        line for line in transcript_text.splitlines() if line.strip()
    ]
    haystack = _normalise(transcript_text)

    errors: list[str] = []
    kept: list[BoardRequest] = []
    dropped: list[str] = []

    for index, item in enumerate(items):
        where = f"requests[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{where}: must be an object, got {type(item).__name__}")
            continue

        intent = _text_field(item, "intent", where, errors)
        quote = _text_field(item, "quote", where, errors)
        confidence = _coerce_confidence(item.get("confidence"), where, errors)
        speaker = item.get("speaker")
        speaker = speaker.strip() if isinstance(speaker, str) else ""

        if not intent or not quote or confidence is None:
            continue

        # Length bounds before containment: both ends of the range make the
        # containment test meaningless rather than merely wrong.
        if len(quote) < MIN_QUOTE_CHARS:
            errors.append(
                f"{where}: 'quote' is too short to identify a line "
                f"({len(quote)} chars, need {MIN_QUOTE_CHARS})"
            )
            continue
        if len(quote) > MAX_QUOTE_CHARS:
            errors.append(
                f"{where}: 'quote' is {len(quote)} chars; a quote over "
                f"{MAX_QUOTE_CHARS} is the transcript, not a citation"
            )
            continue

        # The hallucination filter. Not an error -- a model that invents one
        # request out of three has still done useful work on the other two,
        # and failing the whole batch would throw those away. It is a drop,
        # and the count is reported below if it turns out to be all of them.
        if _normalise(quote) not in haystack:
            dropped.append(f"{where}: quote is not in the transcript: {quote[:80]!r}")
            continue

        kept.append(
            BoardRequest(
                intent=intent,
                quote=quote,
                confidence=confidence,
                speaker=speaker or _speaker_of(transcript_lines, quote),
            )
        )

    if errors:
        raise IntentError(errors)

    # Every request the model gave was unsupported by the transcript. Returning
    # [] here would read as "this meeting asked for nothing", which is the
    # opposite of what happened: the model claimed it asked for several things
    # and could not back up any of them. That is a broken answer, not a quiet
    # one.
    if items and not kept:
        raise IntentError(
            [f"every request was unverifiable ({len(dropped)} dropped)"] + dropped
        )

    # Stable sort on confidence alone, so equal-confidence requests keep the
    # order the model gave them rather than an arbitrary one that could differ
    # between runs of the same transcript.
    kept.sort(key=lambda request: request.confidence, reverse=True)
    return kept[:max_requests]

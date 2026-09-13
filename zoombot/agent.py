"""Live speech in, board requests out.

The post-hoc Meet front end (:mod:`meetings.intent`) is handed one finished
transcript and asks a model which board requests it contains. Zoom's Realtime
Media Streams hand this package the same content in *fragments* -- one
:class:`~zoombot.rtms.TranscriptChunk` per utterance, arriving while the
meeting is still happening. That is the only difference, and it is entirely a
windowing problem: what is a "transcript" worth asking a model about, when the
transcript is still being written?

**Nothing about the safety gates is re-derived here.** The extraction, the
batched validation and -- above all -- the hallucination filter (a request
whose quote is not literally in the transcript is dropped) all belong to
:func:`meetings.intent.extract_requests`, which this module calls. That filter
is the whole reason a meeting is safe to feed a pipeline at all: downstream
there is no human in the loop, and the next thing that happens is a paid run.
Re-implementing it here would mean maintaining it twice and getting it wrong
once.

**Windowing, stated rather than buried.** :func:`window_chunks` is a pure
function over the chunk list, with every threshold a module constant, so the
policy is testable without a model:

* a window ends at :data:`WINDOW_GAP_MS` of silence -- a long pause is the
  cheapest available signal that a topic finished;
* a window ends at :data:`WINDOW_MAX_CHARS` regardless, so one uninterrupted
  monologue cannot grow into an unbounded prompt;
* the last :data:`WINDOW_OVERLAP_CHUNKS` chunks of a window are repeated at the
  head of the next one, because a requirement stated across a boundary would
  otherwise be quotable from neither half;
* a window under :data:`WINDOW_MIN_CHARS` is not sent at all. It cannot contain
  a quotable request (``meetings.intent`` refuses a quote under 12 characters
  for the same reason), and each window is a paid model call.

The overlap makes duplicates certain rather than merely possible, so requests
are de-duplicated on their quote before the cap is applied -- otherwise one
sentence said once could spend two pipeline runs.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from meetings.intent import BoardRequest, IntentError, extract_requests

from .rtms import TranscriptChunk

__all__ = [
    "Window",
    "requests_from_chunks",
    "window_chunks",
    "WINDOW_GAP_MS",
    "WINDOW_MAX_CHARS",
    "WINDOW_MIN_CHARS",
    "WINDOW_OVERLAP_CHUNKS",
]

log = logging.getLogger("zoombot.agent")

#: The gap between two successive chunk timestamps, in milliseconds, that ends
#: a window. ``at_ms`` marks when a chunk started, so this is silence plus the
#: length of the previous utterance -- an approximation, and deliberately a
#: generous one: long enough that a speaker drawing breath mid-requirement does
#: not split their own sentence in half, short enough that two unrelated agenda
#: items rarely share a window.
WINDOW_GAP_MS = 15_000

#: Hard ceiling on one window's transcript text. A window is one prompt, and a
#: forty-minute uninterrupted stream must not become a forty-minute prompt.
WINDOW_MAX_CHARS = 4_000

#: Below this a window is not sent to the model at all. ``meetings.intent``
#: refuses any quote shorter than 12 characters because containment proves
#: nothing about it, so a window of that size cannot produce a surviving
#: request -- and every window costs a model call.
WINDOW_MIN_CHARS = 40

#: How many chunks from the end of a window are repeated at the start of the
#: next. Without this, "we need a small 3.3 volt regulator board" split across
#: a boundary is quotable from neither window and the request vanishes with no
#: error anywhere.
WINDOW_OVERLAP_CHUNKS = 2


@dataclass(frozen=True)
class Window:
    """A contiguous slice of the stream, and its transcript rendering.

    ``text`` is exactly what the model sees and exactly what quotes are checked
    against, so the two can never drift apart.
    """

    chunks: tuple[TranscriptChunk, ...] = field(default_factory=tuple)

    @property
    def text(self) -> str:
        """``speaker: text`` lines, the shape ``meetings.intent`` expects."""
        return "\n".join(
            f"{chunk.speaker or 'participant'}: {chunk.text.strip()}"
            for chunk in self.chunks
        )

    @property
    def meeting_id(self) -> str:
        return self.chunks[0].meeting_id if self.chunks else ""

    def __len__(self) -> int:
        return len(self.chunks)


def _usable(chunks: Iterable[TranscriptChunk]) -> list[TranscriptChunk]:
    """Drop chunks with no words in them.

    RTMS emits keep-alive and partial-result frames whose text is empty; they
    carry timing, not speech, and letting them through would make the silence
    gap measure the wrong thing.
    """
    return [chunk for chunk in chunks if chunk.text and chunk.text.strip()]


def window_chunks(
    chunks: Iterable[TranscriptChunk],
    *,
    gap_ms: int = WINDOW_GAP_MS,
    max_chars: int = WINDOW_MAX_CHARS,
    overlap: int = WINDOW_OVERLAP_CHUNKS,
) -> list[Window]:
    """Split a fragmented stream into windows worth asking a model about.

    Pure and deterministic: the same chunks always give the same windows, which
    is what makes this policy testable rather than an emergent property of a
    loop. Windows below :data:`WINDOW_MIN_CHARS` are *not* filtered here --
    that is :func:`requests_from_chunks`'s decision about spending money, and
    keeping it out of the split keeps the split honest about what was said.
    """
    if gap_ms < 0:
        raise ValueError(f"gap_ms must not be negative, got {gap_ms}")
    if max_chars < 1:
        raise ValueError(f"max_chars must be at least 1, got {max_chars}")
    if overlap < 0:
        raise ValueError(f"overlap must not be negative, got {overlap}")

    usable = _usable(chunks)
    windows: list[Window] = []
    current: list[TranscriptChunk] = []
    size = 0
    previous_end: int | None = None

    def flush() -> None:
        nonlocal current, size
        if current:
            windows.append(Window(tuple(current)))
            # Carry the tail forward so a sentence spanning the boundary stays
            # quotable. The carried chunks count towards the next window's
            # size, so the ceiling still holds.
            current = current[-overlap:] if overlap else []
            size = sum(len(chunk.text) for chunk in current)
            if size >= max_chars:
                # The carried tail alone fills a window. Keeping it would make
                # every following window mostly repeat, so it is dropped: the
                # ceiling matters more than the overlap.
                current = []
                size = 0

    for chunk in usable:
        gapped = previous_end is not None and chunk.at_ms - previous_end >= gap_ms
        too_big = size and size + len(chunk.text) > max_chars
        if gapped or too_big:
            flush()
            if gapped:
                # A pause ends the topic, so the overlap would carry words from
                # the wrong conversation into the next window.
                current = []
                size = 0
        current.append(chunk)
        size += len(chunk.text)
        previous_end = chunk.at_ms

    if current:
        windows.append(Window(tuple(current)))
    return windows


def _quote_key(text: str) -> str:
    """The identity of a quote for de-duplication.

    Must match ``meetings.intent``'s containment normalisation -- whitespace
    collapsed, case folded, nothing else -- or the overlap between two windows
    would produce two "different" requests for one sentence.
    """
    return re.sub(r"\s+", " ", text).strip().casefold()


def requests_from_chunks(
    model,
    chunks: Iterable[TranscriptChunk],
    *,
    max_requests: int = 3,
    warnings: list[str] | None = None,
) -> list[BoardRequest]:
    """Find the board requests in a live transcript stream.

    Returns at most ``max_requests`` requests, highest confidence first, each
    quoting a line genuinely present in the window it came from.

    ``warnings`` is an optional sink. A window whose model answer failed
    validation is one window's problem, not the meeting's: the other windows
    have already produced usable requests or will, and abandoning them would
    lose real requirements to one bad answer. The failure is appended here
    instead of disappearing -- and if *every* window failed and nothing
    survived, :class:`~meetings.intent.IntentError` is raised with all of them
    batched into it, because "no requests" must never be readable as "the
    meeting asked for nothing" when in fact nothing could be read at all.

    :class:`~silkscreen.agents.model.ModelError` is deliberately not caught: a
    call that never happened is not a meeting without requirements.
    """
    if max_requests < 1:
        raise ValueError(f"max_requests must be at least 1, got {max_requests}")

    windows = window_chunks(chunks)
    problems: list[str] = []
    asked = 0
    kept: list[BoardRequest] = []
    seen: set[str] = set()

    for index, window in enumerate(windows):
        text = window.text
        if len(text) < WINDOW_MIN_CHARS:
            log.debug("window %d is %d chars; not worth a call", index, len(text))
            continue
        asked += 1
        try:
            found = extract_requests(model, text, max_requests=max_requests)
        except IntentError as exc:
            problems.extend(f"window {index}: {message}" for message in exc.errors)
            continue
        for request in found:
            key = _quote_key(request.quote)
            if key in seen:
                continue
            seen.add(key)
            kept.append(request)

    if problems and not kept:
        # Every window that was asked came back unusable. That is a broken
        # answer, not a quiet one.
        raise IntentError(problems)
    if problems and warnings is not None:
        warnings.extend(problems)
    if problems:
        log.warning("%d window(s) produced unusable answers", len(problems))
    if warnings is not None and asked == 0 and windows:
        warnings.append(
            f"{len(windows)} window(s) of speech were all shorter than "
            f"{WINDOW_MIN_CHARS} characters; nothing was sent to the model"
        )

    kept.sort(key=lambda request: request.confidence, reverse=True)
    return kept[:max_requests]

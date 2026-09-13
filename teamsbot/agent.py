"""Teams speech -> board requests, through the gates that already exist.

This module deliberately owns **no** extraction logic of its own. The hard part
-- treating model output as an untrusted claim about a transcript, batching
every validation failure into one error, and throwing away any request whose
quote is not literally in the source -- was solved once in
:mod:`meetings.intent` and is reused here verbatim. A second implementation of
those gates would be a second place for them to drift, and the gate that drifts
is the one that lets a hallucinated requirement through to a paid pipeline run.

What is genuinely new here is **time**. Meet hands over one finished
transcript; a Teams calling bot hands over a stream of fragments, and a stated
requirement rarely fits inside one of them:

    14:02:11  amira: okay so for the sensor rig
    14:02:13  amira: we need a little 3.3 volt regulator board
    14:02:16  amira: usb-c in, and it has to fit the existing enclosure

Fed to the extractor one fragment at a time, that request exists in none of
them. Fed as one ever-growing document, the extractor re-reports the same
request on every chunk and each report is a paid run. So the fragments are
gathered into **windows** first, and the windowing is a named, documented,
testable policy object rather than a magic number inside a loop:
:class:`WindowPolicy` and :func:`windows`.

The three rules that make a window boundary safe:

* **Overlap.** Consecutive windows share a trailing tail, because a request
  split across a boundary would otherwise be quotable from neither side. An
  overlap that is not strictly smaller than the span would never advance, so
  that is a construction-time error rather than a hang.
* **Silence closes a window early.** A long pause is a topic change, and
  merging speech across it invents context nobody supplied.
* **Deduplication after the fact.** Overlap means the same sentence is offered
  to the model twice, so identical quotes are collapsed, keeping the higher
  confidence. Without this, overlap would double the bill.

Nothing here returns a quiet zero. An empty list means the windows were read
and contained no hardware request. A model that failed on every window raises,
because "no requests" and "nothing worked" must never look the same.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from meetings.intent import BoardRequest, IntentError, extract_requests

if TYPE_CHECKING:  # pragma: no cover - typing only
    from silkscreen.agents.model import Model

__all__ = [
    "Chunk",
    "Window",
    "WindowPolicy",
    "DEFAULT_WINDOW_POLICY",
    "windows",
    "transcript_from_chunks",
    "requests_from_chunks",
    "requests_from_transcript",
    "dedupe",
]


@runtime_checkable
class Chunk(Protocol):
    """One fragment of live transcription.

    Structural on purpose: ``teamsbot.graph.TranscriptChunk`` is the concrete
    type in production, but the windowing below is pure arithmetic over
    ``at_ms`` and text, and pinning it to one class would make it untestable
    without the transport package and unusable for the Zoom half, which yields
    a chunk of exactly this shape from a different module.
    """

    speaker: str
    text: str
    at_ms: int


@dataclass(frozen=True)
class WindowPolicy:
    """How live fragments are gathered before a model ever sees them.

    Every number is here rather than inline so it can be stated, justified and
    changed in one place -- and so a test can shrink the window to two seconds
    instead of simulating ninety.
    """

    #: Longest stretch of wall-clock speech in one window. A requirement and
    #: its qualifiers ("...and it needs USB-C") land within about a minute and
    #: a half of each other; a longer window only buys the model more unrelated
    #: material to build a plausible request out of.
    span_ms: int = 90_000

    #: Trailing tail carried into the next window. A sentence that straddles a
    #: boundary is quotable from neither side without this, which is the one
    #: failure mode windowing introduces that streaming did not have.
    overlap_ms: int = 20_000

    #: A gap at least this long closes the window early. People stop talking
    #: between topics; stitching across the pause merges two subjects into one
    #: "request" that nobody stated.
    silence_ms: int = 15_000

    #: Character bound on one window's transcript, so an unusually dense
    #: stretch cannot produce an unbounded prompt. Independent of ``span_ms``
    #: because chunk size is the transport's business, not the clock's.
    max_chars: int = 4_000

    def __post_init__(self) -> None:
        problems: list[str] = []
        if self.span_ms <= 0:
            problems.append(f"span_ms must be positive, got {self.span_ms}")
        if self.overlap_ms < 0:
            problems.append(f"overlap_ms must not be negative, got {self.overlap_ms}")
        if self.overlap_ms >= self.span_ms:
            # Not a warning: with overlap >= span the next window starts at or
            # before the current one, and the loop below never advances.
            problems.append(
                f"overlap_ms ({self.overlap_ms}) must be smaller than span_ms "
                f"({self.span_ms}); an overlap that wide never advances"
            )
        if self.silence_ms <= 0:
            problems.append(f"silence_ms must be positive, got {self.silence_ms}")
        if self.max_chars <= 0:
            problems.append(f"max_chars must be positive, got {self.max_chars}")
        if problems:
            raise ValueError("; ".join(problems))


DEFAULT_WINDOW_POLICY = WindowPolicy()


@dataclass(frozen=True)
class Window:
    """A contiguous run of fragments, and the transcript they make together."""

    chunks: tuple[Chunk, ...]

    @property
    def start_ms(self) -> int:
        return self.chunks[0].at_ms if self.chunks else 0

    @property
    def end_ms(self) -> int:
        return self.chunks[-1].at_ms if self.chunks else 0

    def transcript(self) -> str:
        """``speaker: text`` lines -- the shape :mod:`meetings.intent` reads.

        The same format the Meet client produces, so the extractor's speaker
        recovery and quote containment behave identically on both sources.
        """
        return transcript_from_chunks(self.chunks)


def transcript_from_chunks(chunks: Iterable[Chunk]) -> str:
    """Render fragments as a transcript, one ``speaker: text`` line each.

    Consecutive fragments from the same speaker are joined into one line: live
    transcription splits a sentence mid-clause, and a request broken across
    three lines with the same prefix is harder for both a model and a human to
    quote than the sentence it actually was.
    """
    lines: list[str] = []
    last_speaker: str | None = None
    for chunk in chunks:
        text = (chunk.text or "").strip()
        if not text:
            continue
        speaker = (chunk.speaker or "").strip()
        if speaker and speaker == last_speaker and lines:
            lines[-1] = f"{lines[-1]} {text}"
            continue
        lines.append(f"{speaker}: {text}" if speaker else text)
        last_speaker = speaker or None
    return "\n".join(lines)


def windows(
    chunks: Iterable[Chunk],
    *,
    policy: WindowPolicy = DEFAULT_WINDOW_POLICY,
) -> list[Window]:
    """Gather fragments into overlapping windows under ``policy``.

    A window closes when the next fragment would push it past ``span_ms``, when
    it would push the transcript past ``max_chars``, or when the gap since the
    previous fragment reaches ``silence_ms``. The next window then restarts
    from the first fragment within ``overlap_ms`` of the closed window's end,
    which is what makes a boundary survivable for a sentence that crosses it.

    Fragments are sorted by ``at_ms``: a stream may deliver two speakers'
    fragments out of order, and a window built in arrival order would then
    report a span it does not have.
    """
    ordered = sorted(
        (c for c in chunks if (c.text or "").strip()),
        key=lambda c: c.at_ms,
    )
    if not ordered:
        return []

    out: list[Window] = []
    index = 0
    while index < len(ordered):
        current: list[Chunk] = [ordered[index]]
        chars = len(ordered[index].text)
        cursor = index + 1
        while cursor < len(ordered):
            nxt = ordered[cursor]
            if nxt.at_ms - current[0].at_ms > policy.span_ms:
                break
            if nxt.at_ms - current[-1].at_ms >= policy.silence_ms:
                break
            if chars + len(nxt.text) > policy.max_chars and len(current) > 1:
                # ``len(current) > 1``: a single fragment longer than the bound
                # still becomes its own window. Dropping it would lose speech,
                # which is worse than one oversized prompt, and the extractor
                # truncates from its own end anyway.
                break
            current.append(nxt)
            chars += len(nxt.text)
            cursor += 1

        out.append(Window(chunks=tuple(current)))
        if cursor >= len(ordered):
            break

        # Where the next window starts: the earliest fragment *strictly* inside
        # the overlap tail, but never earlier than index+1, or the loop stalls.
        # Strictly, because ``overlap_ms=0`` has to mean no overlap at all --
        # with ``>=`` the last fragment of every window would be carried
        # forward regardless, and the policy would be unable to express the
        # non-overlapping case the tests use as the control.
        tail_from = current[-1].at_ms - policy.overlap_ms
        nxt_index = cursor
        for back in range(cursor - 1, index, -1):
            if ordered[back].at_ms > tail_from:
                nxt_index = back
            else:
                break
        index = max(nxt_index, index + 1)

    return out


def _normalise(text: str) -> str:
    """Whitespace-and-case-insensitive form, for duplicate detection only.

    Deliberately a local copy of the idea in :mod:`meetings.intent` rather than
    an import of its private helper: this one answers "are these two model
    answers about the same sentence", which is a different question from "did
    the speaker actually say this", and the two must be free to diverge.
    """
    return re.sub(r"\s+", " ", text).strip().casefold()


def dedupe(requests: Iterable[BoardRequest]) -> list[BoardRequest]:
    """Collapse requests that quote the same sentence, keeping the best one.

    Overlapping windows offer the same speech to the model twice by design, so
    without this every straddling request is built twice -- two paid runs, two
    boards, one requirement. Ties keep the first occurrence, so the result is
    stable for a given stream.
    """
    best: dict[str, BoardRequest] = {}
    order: list[str] = []
    for request in requests:
        key = _normalise(request.quote)
        current = best.get(key)
        if current is None:
            best[key] = request
            order.append(key)
        elif request.confidence > current.confidence:
            best[key] = request
    return [best[key] for key in order]


def requests_from_transcript(
    model: Model,
    transcript_text: str,
    *,
    max_requests: int = 3,
) -> list[BoardRequest]:
    """The post-hoc path: one finished ``callTranscript``, read once.

    A pass-through to :func:`meetings.intent.extract_requests` on purpose --
    the gates are the point, and this function exists so a caller reading a
    Graph transcript does not have to know which package they live in.
    """
    return extract_requests(model, transcript_text, max_requests=max_requests)


def requests_from_chunks(
    model: Model,
    chunks: Iterable[Chunk],
    *,
    max_requests: int = 3,
    policy: WindowPolicy = DEFAULT_WINDOW_POLICY,
    on_warning: Callable[[str], None] | None = None,
) -> list[BoardRequest]:
    """The live path: fragments in, verified board requests out.

    Every window is extracted independently, so a quote is checked against the
    window it was drawn from rather than against the whole meeting -- a model
    cannot borrow a phrase from ten minutes earlier to justify a request here.

    One window that fails validation does not abandon the rest: people talk
    over each other, and a single unparseable answer must not lose the request
    stated thirty seconds later. Each failure is reported through
    ``on_warning`` if given. But if a window failed and **nothing at all**
    survived, the failures are raised as one
    :class:`~meetings.intent.IntentError`: an empty list in that situation
    would read as "the meeting asked for nothing", when what actually happened
    is that the only evidence was unreadable.
    """
    if max_requests < 1:
        raise ValueError(f"max_requests must be at least 1, got {max_requests}")

    found: list[BoardRequest] = []
    failures: list[str] = []
    panes = windows(chunks, policy=policy)

    for number, window in enumerate(panes, start=1):
        where = f"window {number}/{len(panes)} ({window.start_ms}-{window.end_ms}ms)"
        try:
            found.extend(
                extract_requests(
                    model,
                    window.transcript(),
                    # Per-window cap, not the global one: the global cap is
                    # applied once at the end, after duplicates from the
                    # overlap have been collapsed. Capping twice would silently
                    # discard a request that only looked like a surplus.
                    max_requests=max_requests,
                )
            )
        except IntentError as exc:
            failures.append(f"{where}: {exc}")
            if on_warning is not None:
                on_warning(f"could not read {where}: {exc}")

    if failures and not found:
        raise IntentError(
            [
                f"no request survived; {len(failures)} of {len(panes)} window(s) "
                f"failed and the rest found nothing",
                *failures,
            ]
        )

    merged = dedupe(found)
    # Stable sort on confidence alone: equal-confidence requests keep the order
    # the stream produced them in, so the same recording gives the same answer.
    merged.sort(key=lambda request: request.confidence, reverse=True)
    return merged[:max_requests]

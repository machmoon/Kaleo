"""Turn a finished run into a short agenda, or say plainly that it could not.

The :mod:`silkscreen.agents.sourcing` shape applied to the end of a run.
The model is shown what the run actually produced -- the reviewer's
findings, the nets the router left as ratsnest, the enclosure kernel
clauses that failed with their signed margins, the BOM rows nobody could
source -- and asked for the agenda in
:mod:`silkscreen.specreview`'s vocabulary. Its answer goes through
:func:`~silkscreen.specreview.parse_spec_review`, every failure batched
back as one repair prompt, and after at most ``max_repairs`` rounds the
loop **gives up loudly**: no review, one warning naming the failure. A
half-parsed agenda would book a meeting about the wrong thing, which is
worse than booking none.

Two rules decide whether this is honest, and both are enforced here:

* Items naming parts or nets the board does not contain are dropped by the
  parser's filter (``known_refs``) and each drop is reported. The
  :func:`~silkscreen.agents.review.review_circuit` rule, applied again --
  an agenda item pointing at ``R9`` on a board with eight resistors sends
  a person hunting for something that was never there.
* An agenda with no blocking item is a **result**, not a gap: it means
  nothing in this run needs a meeting. The model is told so in the prompt
  and the empty answer is returned unchanged. Nothing here invents an item
  to fill a slot, and a run with no evidence at all does not call the
  model at all.

Intended live tier: the reasoning tier rather than
:data:`~silkscreen.agents.model.CHEAP_MODEL` -- deciding which of five
defects is worth a human's twenty minutes is a judgement pass. The tier is
the caller's to construct; this module only ever sees the :class:`Model`
protocol.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from ..specreview import (
    MAX_ITEMS,
    MAX_TOTAL_MINUTES,
    MIN_TOTAL_MINUTES,
    SOURCES,
    SUMMARY_MAX_CHARS,
    SpecReview,
    SpecReviewResult,
    SpecReviewValidationError,
    parse_spec_review,
)
from .model import Model

__all__ = [
    "SPECREVIEW_MARKER",
    "SPECREVIEW_PROMPT",
    "evidence_block",
    "propose_spec_review",
]

#: Frozen (the spec-review contract): appears verbatim in every prompt so
#: any ``ScriptedModel.by_marker`` can key on it.
SPECREVIEW_MARKER = "SPEC-REVIEW-AGENDA v1"

#: How much of the batched error list a give-up warning carries.
_WARNING_CHARS = 400

#: How much of one piece of evidence reaches the prompt. Bounded because a
#: reviewer's ``detail`` is prose and the whole point is not to ship prose.
_EVIDENCE_CHARS = 300

#: Evidence lines per source. A board with forty unrouted nets needs a
#: meeting about routing, not forty agenda items, and the count is stated
#: in the block either way.
_MAX_PER_SOURCE = 12

SPECREVIEW_PROMPT = f"""\
You are a hardware lead preparing a design review meeting for a PCB a
colleague just generated ({SPECREVIEW_MARKER}). You are given everything the
run flagged. Decide what actually needs a human in a room, and write the
agenda. Respond with ONE JSON object -- no prose, no code fence:

{{
  "title": "<short name for the meeting>",
  "summary": "<what this board is and what state it is in>",
  "items": [
    {{"topic": "<the decision, as a question or a noun phrase>",
     "why": "<why a person has to decide this, citing the evidence given>",
     "minutes": <whole minutes>,
     "blocking": true|false,
     "refs": ["<part refs and net names this item is about>"]}}
  ],
  "decisions_needed": ["<one line per decision that must leave the room>"],
  "prepared_from": [{", ".join(f'"{s}"' for s in SOURCES)}]
}}

Hard rules -- an answer breaking any of these is rejected automatically:

1. "summary" is at most {SUMMARY_MAX_CHARS} characters. This meeting exists
   because nobody reads the full report; do not write one here.
2. Between 1 and {MAX_ITEMS} items, totalling between {MIN_TOTAL_MINUTES}
   and {MAX_TOTAL_MINUTES} minutes. Merge related defects into one item
   rather than listing them.
3. "blocking" is true only when the board cannot ship until this is
   decided. If nothing is blocking, say so -- an agenda of non-blocking
   items is a fine answer and means "no meeting needed yet".
4. Only name refs that appear in the evidence below. An item about a part
   that is not there is discarded, so it costs you the item.
5. "prepared_from" names only the stages the evidence below came from.
6. Do not invent problems. If the evidence is thin, the agenda is short.
"""


def _clip(text: object, limit: int = _EVIDENCE_CHARS) -> str:
    """One line of evidence, bounded, newlines flattened."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _lines(items: Sequence[str], label: str) -> list[str]:
    """A bounded block, which says how much it left out rather than
    trimming silently."""
    out = [f"  - {line}" for line in items[:_MAX_PER_SOURCE]]
    if len(items) > _MAX_PER_SOURCE:
        out.append(
            f"  - ({len(items) - _MAX_PER_SOURCE} further {label} not listed)"
        )
    return out


def evidence_block(
    *,
    findings: Sequence[Any] = (),
    unrouted: Mapping[str, str] | None = None,
    clauses: Sequence[Any] = (),
    sourcing: Any = None,
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """Render the run's evidence for the prompt.

    Returns the text, the sources that contributed (a subset of
    :data:`~silkscreen.specreview.SOURCES`, in that order), and every ref
    or net name the evidence mentioned -- which is what the parser's
    hallucination filter is keyed on, so an item may only be about
    something the model was actually shown.

    Everything is duck-typed on purpose: a reviewer
    :class:`~silkscreen.agents.review.Finding`, a
    :class:`~silkscreen.enclosure.kernel.Clause` and a
    :class:`~silkscreen.sourcing.SourcingEntry` come from three layers that
    do not import each other, and this module refuses to be the place that
    joins them.
    """
    sections: list[str] = []
    sources: list[str] = []
    refs: list[str] = []

    if findings:
        rows = []
        for finding in findings:
            parts = tuple(getattr(finding, "parts", ()) or ())
            refs.extend(str(p) for p in parts)
            where = f" [{', '.join(str(p) for p in parts)}]" if parts else ""
            severity = getattr(finding, "severity", "note")
            severity = getattr(severity, "value", severity)
            rows.append(
                f"{str(severity).upper()}{where}: "
                f"{_clip(getattr(finding, 'title', finding))}"
                + (
                    f" -- {_clip(getattr(finding, 'detail', ''))}"
                    if getattr(finding, "detail", "")
                    else ""
                )
            )
        sections.append(
            "Design review findings:\n" + "\n".join(_lines(rows, "findings"))
        )
        sources.append("review")

    if unrouted:
        refs.extend(str(net) for net in unrouted)
        rows = [f"net {net}: {_clip(reason)}" for net, reason in unrouted.items()]
        sections.append(
            "Nets the router left as ratsnest:\n" + "\n".join(_lines(rows, "nets"))
        )
        sources.append("route")

    failed = [c for c in clauses if not getattr(c, "passed", True)]
    if failed:
        rows = []
        for clause in failed:
            name = str(getattr(clause, "name", clause))
            refs.append(name)
            rows.append(
                f"clause {name} failed by "
                f"{getattr(clause, 'margin_nm', 0) / 1e6:+.3f} mm: "
                f"{_clip(getattr(clause, 'detail', ''))}"
            )
        sections.append(
            "Enclosure kernel clauses that failed:\n"
            + "\n".join(_lines(rows, "clauses"))
        )
        sources.append("kernel")

    if sourcing is not None:
        parts = list(getattr(sourcing, "parts", ()) or ())
        gaps = [p for p in parts if getattr(p, "mpn_status", "none") == "none"]
        unverified = [
            p
            for p in parts
            if getattr(p, "datasheet_url", None)
            and getattr(p, "datasheet_status", "none") != "verified"
        ]
        rows = []
        for part in gaps:
            ref = str(getattr(part, "ref", ""))
            refs.append(ref)
            rows.append(
                f"{ref}: no part number found "
                f"({_clip(getattr(part, 'value', '') or 'no value')}, "
                f"{_clip(getattr(part, 'package', ''))})"
            )
        for part in unverified:
            ref = str(getattr(part, "ref", ""))
            refs.append(ref)
            rows.append(
                f"{ref}: datasheet link is "
                f"{getattr(part, 'datasheet_status', 'none')}, not confirmed"
            )
        for warning in getattr(sourcing, "warnings", ()) or ():
            rows.append(f"sourcing warning: {_clip(warning)}")
        if rows:
            sections.append(
                "Parts with sourcing gaps:\n" + "\n".join(_lines(rows, "rows"))
            )
            sources.append("sourcing")

    text = "\n\n".join(sections)
    return text, tuple(sources), tuple(dict.fromkeys(r for r in refs if r))


def _nothing_to_discuss(board_summary: str) -> SpecReview:
    """The run flagged nothing. A stated result, not an empty value.

    No model call is made: there is nothing to reason about, and asking a
    model to summarise an empty list is how an invented agenda gets born.
    """
    return SpecReview(
        title="No spec review needed",
        summary=(
            _clip(board_summary, SUMMARY_MAX_CHARS - 80) + " "
            if board_summary
            else ""
        )
        + "The run flagged nothing that needs human judgement: "
        "no review findings, no unrouted nets, no failed enclosure "
        "clauses and no sourcing gaps.",
        items=(),
        decisions_needed=(),
        prepared_from=(),
    )


def propose_spec_review(
    model: Model,
    *,
    board_summary: str = "",
    findings: Sequence[Any] = (),
    unrouted: Mapping[str, str] | None = None,
    clauses: Sequence[Any] = (),
    sourcing: Any = None,
    known_refs: Iterable[str] | None = None,
    max_repairs: int = 1,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> SpecReviewResult:
    """Ask the model for the agenda this run needs; never guess one.

    One model call, plus at most ``max_repairs`` more when the answer fails
    :func:`~silkscreen.specreview.parse_spec_review` -- the batched errors
    go back as a single repair prompt each time. When the budget is spent
    the result carries ``review=None`` and one warning naming the failure:
    no agenda is honest, a half-parsed one is not.

    ``known_refs`` is the vocabulary the agenda may talk about. Left unset
    it defaults to every ref and net name the evidence itself mentioned, so
    an item about a part the model was never shown is dropped and said so
    in :attr:`~silkscreen.specreview.SpecReview.dropped`.

    With no evidence at all the model is not called: the result is a review
    with no items, meaning "nothing needs a meeting". That is a legitimate
    answer and :attr:`~silkscreen.specreview.SpecReviewResult.ok` stays
    true, which is what keeps it distinguishable from a model failure --
    and from never having run the stage, which produces no result at all.

    ``on_event`` receives ``specreview.round`` per rejected answer, one
    ``specreview.dropped`` per filtered item, and a final
    ``specreview.agenda`` -- counts and minutes, never the model's text.

    Raises:
        ModelError: the model could not be reached. Deliberately not
            wrapped -- an outage is a different condition from a bad
            answer, and callers route them differently (the
            :func:`~silkscreen.agents.propose.propose_circuit` convention).
    """
    evidence, sources, seen_refs = evidence_block(
        findings=findings, unrouted=unrouted, clauses=clauses, sourcing=sourcing
    )
    if not sources:
        return SpecReviewResult(review=_nothing_to_discuss(board_summary))

    vocabulary = seen_refs if known_refs is None else tuple(known_refs)

    board = f"The board:\n  {_clip(board_summary)}\n\n" if board_summary else ""
    base = (
        f"{SPECREVIEW_PROMPT}\n{board}What this run flagged "
        f"(stages: {', '.join(sources)}):\n{evidence}\n"
    )
    prompt = base

    review: SpecReview | None = None
    last_errors: list[str] = []
    raw = ""
    for round_no in range(max_repairs + 1):
        # A transport failure is NOT wrapped: ModelError propagates so a
        # FallbackModel's failover -- and the service's 502 -- stay intact.
        raw = model.generate(prompt, temperature=0.0, max_output_tokens=4096)
        try:
            review = parse_spec_review(raw, known_refs=vocabulary)
        except SpecReviewValidationError as exc:
            last_errors = list(exc.errors)
        else:
            break
        if on_event is not None:
            on_event(
                {
                    "event": "specreview.round",
                    "round": round_no + 1,
                    "errors": len(last_errors),
                    "first_error": str(last_errors[0])[:160] if last_errors else "",
                }
            )
        if round_no == max_repairs:
            break
        problems = "\n".join(f"  - {e}" for e in last_errors)
        prompt = (
            f"{base}\nYour previous answer was rejected. Fix ALL of these "
            f"and return the corrected JSON object:\n{problems}\n\n"
            f"Your previous answer was:\n{raw}\n"
        )

    if review is None:
        detail = "; ".join(last_errors)[:_WARNING_CHARS]
        return SpecReviewResult(
            review=None,
            warnings=[
                f"no spec review agenda: no usable answer after "
                f"{max_repairs + 1} attempt(s) ({detail})"
            ],
        )

    warnings = list(review.dropped)
    if on_event is not None:
        for message in review.dropped:
            on_event({"event": "specreview.dropped", "detail": message[:160]})
        on_event(
            {
                "event": "specreview.agenda",
                "items": len(review.items),
                "blocking": len(review.blocking),
                "minutes": review.total_minutes(),
                "needs_meeting": review.needs_meeting,
            }
        )
    return SpecReviewResult(review=review, warnings=warnings)

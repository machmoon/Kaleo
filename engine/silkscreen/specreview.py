"""The agenda a finished run hands to a human, as validated model output.

A run ends with things a machine cannot decide: a blocker the reviewer
raised, three nets the router left as ratsnest, a kernel clause that failed
by 0.4 mm, a part nobody could source. The old answer was to print all of
it and hope somebody read it. This module is the other answer -- the run
proposes a **short agenda**, and the agenda becomes a calendar hold with a
Meet link so the open questions get decided by people, in a meeting, with
the evidence in front of them.

That only works if the agenda is short and true, which is what the
validation here is for. It follows :mod:`silkscreen.netlist` throughout: a
fenced JSON answer is tolerated, and **every** failure is collected into
one :class:`SpecReviewValidationError` so the whole batch goes back to the
model as a single repair prompt rather than one round trip per mistake.

Three bounds are the feature, not decoration:

* ``summary`` is at most :data:`SUMMARY_MAX_CHARS` characters. A wall of
  text is exactly what this replaces; a summary that grows into one has
  quietly turned back into the thing it was built to avoid.
* one to :data:`MAX_ITEMS` items. Nobody decides fifteen things in a
  meeting, and an agenda that long is a report wearing an agenda's hat.
* :data:`MIN_TOTAL_MINUTES` to :data:`MAX_TOTAL_MINUTES` minutes in total.
  Below the floor there is nothing worth booking a room for; above the
  ceiling the model is proposing a workshop, and nobody will accept it.

Two honesty rules live in the layer around this one and are named here
because reading this file is where somebody will look for them:

1. An item whose ``refs`` name nothing the board contains is **dropped, not
   reported** -- the :func:`silkscreen.agents.review.review_circuit` filter
   applied again. Pass ``known_refs`` to :func:`parse_spec_review` to turn
   it on. The bounds above are checked against what the model *proposed*,
   before the filter runs, so the repair prompt talks about the answer the
   model actually gave; the filter may then legitimately leave fewer items
   than the floor, or none at all.
2. A :class:`SpecReview` with no blocking item is a **real, reportable
   result** meaning "nothing here needs a meeting". It is not a failure and
   it is not an empty value to be filled in: never invent an agenda to fill
   a slot. ``review is None`` (the model could not answer), a review whose
   ``items`` are empty (nothing to discuss), and never having run the stage
   at all are three different states and stay distinguishable --
   :class:`SpecReviewResult` is what keeps the first two apart, and a
   caller that never built one is the third.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "MAX_DECISIONS",
    "MAX_ITEMS",
    "MAX_ITEM_MINUTES",
    "MAX_TOTAL_MINUTES",
    "MIN_ITEMS",
    "MIN_TOTAL_MINUTES",
    "SOURCES",
    "SUMMARY_MAX_CHARS",
    "TITLE_MAX_CHARS",
    "TOPIC_MAX_CHARS",
    "WHY_MAX_CHARS",
    "AgendaItem",
    "SpecReview",
    "SpecReviewResult",
    "SpecReviewValidationError",
    "parse_spec_review",
]

#: The anti-wall-of-text rule. This is the point of the feature.
SUMMARY_MAX_CHARS = 400
TITLE_MAX_CHARS = 120
TOPIC_MAX_CHARS = 120
WHY_MAX_CHARS = 400

MIN_ITEMS = 1
MAX_ITEMS = 8
#: A single item longer than the whole meeting is a mis-typed number, not
#: an agenda item, and it is worth saying so before the total is summed.
MAX_ITEM_MINUTES = 60
MIN_TOTAL_MINUTES = 15
MAX_TOTAL_MINUTES = 60
MAX_DECISIONS = 8

#: Where an agenda item can have come from. Frozen vocabulary: these are
#: the four run stages that produce something a human might have to judge.
SOURCES = ("review", "route", "kernel", "sourcing")


class SpecReviewValidationError(ValueError):
    """A model's agenda is not usable.

    ``errors`` holds one message per problem so the whole batch can go back
    to the model in a single repair prompt (the :mod:`netlist` convention).
    """

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__(
            f"{len(errors)} problem(s) in spec review:\n  - " + "\n  - ".join(errors)
        )


@dataclass(frozen=True)
class AgendaItem:
    """One thing to decide, and how long it is expected to take."""

    topic: str
    why: str
    minutes: int
    blocking: bool
    #: Part refs and net names the item is about. Empty is legitimate --
    #: "do we ship this at all" is about the whole board.
    refs: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "why": self.why,
            "minutes": self.minutes,
            "blocking": self.blocking,
            "refs": list(self.refs),
        }


@dataclass(frozen=True)
class SpecReview:
    """A validated agenda for one run.

    An instance with no ``items`` is a legitimate result meaning nothing in
    the run needs human judgement; :attr:`needs_meeting` is what a caller
    reads rather than the truthiness of the list.
    """

    title: str
    #: At most :data:`SUMMARY_MAX_CHARS` characters.
    summary: str
    items: tuple[AgendaItem, ...] = ()
    decisions_needed: tuple[str, ...] = ()
    #: Which stages the agenda was prepared from; a subset of :data:`SOURCES`.
    prepared_from: tuple[str, ...] = ()
    #: Items removed because their refs named nothing on the board, one
    #: message each. Reported, never silent -- a filter that drops without
    #: saying so is indistinguishable from a model that said nothing.
    dropped: tuple[str, ...] = ()

    def total_minutes(self) -> int:
        return sum(item.minutes for item in self.items)

    @property
    def blocking(self) -> tuple[AgendaItem, ...]:
        return tuple(item for item in self.items if item.blocking)

    @property
    def needs_meeting(self) -> bool:
        """Is there anything here worth booking time for?

        A blocking item is the trigger. No blocking item is a real answer
        ("nothing needs a meeting"), not an absence of one.
        """
        return bool(self.blocking)

    def as_dict(self) -> dict[str, Any]:
        """Every field, JSON-safe, plus the derived numbers a caller would
        otherwise recompute (and could get wrong on its own)."""
        return {
            "title": self.title,
            "summary": self.summary,
            "items": [item.as_dict() for item in self.items],
            "decisions_needed": list(self.decisions_needed),
            "prepared_from": list(self.prepared_from),
            "dropped": list(self.dropped),
            "total_minutes": self.total_minutes(),
            "blocking_count": len(self.blocking),
            "needs_meeting": self.needs_meeting,
        }


@dataclass
class SpecReviewResult:
    """What the spec review stage produced, and what went wrong.

    ``review is None`` means the model never gave a usable answer and
    ``warnings`` says so; a :class:`SpecReview` with empty ``items`` means
    it answered and there is nothing to discuss. A caller that never ran
    the stage has no :class:`SpecReviewResult` at all. Three states, three
    representations, none of them readable as another.
    """

    review: SpecReview | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.review is not None

    @property
    def needs_meeting(self) -> bool:
        """False when there is nothing to discuss *and* when the model
        failed -- never book a meeting off a failure. ``ok`` is what
        separates the two."""
        return self.review is not None and self.review.needs_meeting

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "review": None if self.review is None else self.review.as_dict(),
            "needs_meeting": self.needs_meeting,
            "warnings": list(self.warnings),
        }


def _strip_code_fence(text: str) -> str:
    """Remove a ``` fence if the model wrapped its JSON in one.

    Copied in spirit from :mod:`silkscreen.netlist`: a fenced answer is
    both likely and, unhandled, fatal.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _text(
    value: Any, where: str, limit: int, errors: list[str], *, required: bool = True
) -> str:
    """One string field, checked for type, emptiness and length.

    Each of those is its own message: a model that returned a number and a
    model that returned an essay have made different mistakes and need to
    be told which.
    """
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        errors.append(f"{where} must be a string, got {type(value).__name__}")
        return ""
    text = value.strip()
    if required and not text:
        errors.append(f"{where} is empty")
    if len(text) > limit:
        errors.append(f"{where} is {len(text)} characters, longer than {limit}")
    return text


def _parse_item(raw: Any, index: int, errors: list[str]) -> AgendaItem | None:
    where = f"items[{index}]"
    if not isinstance(raw, dict):
        errors.append(f"{where} must be an object, got {type(raw).__name__}")
        return None

    topic = _text(raw.get("topic"), f"{where}.topic", TOPIC_MAX_CHARS, errors)
    why = _text(raw.get("why"), f"{where}.why", WHY_MAX_CHARS, errors)

    minutes_raw = raw.get("minutes")
    minutes = 0
    # ``bool`` is an ``int`` in Python and ``True`` would silently become a
    # one-minute item.
    if isinstance(minutes_raw, bool) or not isinstance(minutes_raw, int):
        errors.append(
            f"{where}.minutes must be a whole number of minutes, "
            f"got {type(minutes_raw).__name__}"
        )
    elif minutes_raw < 1:
        errors.append(f"{where}.minutes is {minutes_raw}; an item takes at least 1")
    elif minutes_raw > MAX_ITEM_MINUTES:
        errors.append(
            f"{where}.minutes is {minutes_raw}, longer than the whole "
            f"meeting ({MAX_ITEM_MINUTES})"
        )
    else:
        minutes = minutes_raw

    blocking_raw = raw.get("blocking")
    blocking = False
    if not isinstance(blocking_raw, bool):
        errors.append(
            f"{where}.blocking must be true or false, "
            f"got {type(blocking_raw).__name__}"
        )
    else:
        blocking = blocking_raw

    refs_raw = raw.get("refs")
    refs: tuple[str, ...] = ()
    if refs_raw is None:
        refs = ()
    elif not isinstance(refs_raw, list):
        errors.append(
            f"{where}.refs must be a list of part refs or net names, "
            f"got {type(refs_raw).__name__}"
        )
    else:
        cleaned: list[str] = []
        for position, ref in enumerate(refs_raw):
            if not isinstance(ref, str) or not ref.strip():
                errors.append(f"{where}.refs[{position}] must be a non-empty string")
                continue
            cleaned.append(ref.strip())
        refs = tuple(dict.fromkeys(cleaned))

    # An item with problems is still returned: every later item must be
    # checked too (that is the whole point of batching), and the caller
    # discards the lot when ``errors`` is non-empty.
    return AgendaItem(
        topic=topic, why=why, minutes=minutes, blocking=blocking, refs=refs
    )


def _filter_refs(
    items: Sequence[AgendaItem], known: frozenset[str]
) -> tuple[tuple[AgendaItem, ...], tuple[str, ...]]:
    """Drop hallucinated references, and any item left pointing at nothing.

    A model that names ``R9`` on a board with eight resistors has invented
    the item, not mistyped it, so surfacing it would send a person looking
    for a part that does not exist. An item that named no refs at all is
    about the board as a whole and is kept.
    """
    kept: list[AgendaItem] = []
    dropped: list[str] = []
    for item in items:
        if not item.refs:
            kept.append(item)
            continue
        good = tuple(ref for ref in item.refs if ref in known)
        if not good:
            dropped.append(
                f"dropped agenda item {item.topic!r}: it is about "
                f"{', '.join(item.refs)}, which the board does not contain"
            )
            continue
        if len(good) != len(item.refs):
            missing = [ref for ref in item.refs if ref not in known]
            dropped.append(
                f"agenda item {item.topic!r} also named "
                f"{', '.join(missing)}, which the board does not contain"
            )
        kept.append(
            AgendaItem(
                topic=item.topic,
                why=item.why,
                minutes=item.minutes,
                blocking=item.blocking,
                refs=good,
            )
        )
    return tuple(kept), tuple(dropped)


def parse_spec_review(
    raw: str | dict, *, known_refs: Iterable[str] | None = None
) -> SpecReview:
    """Parse and validate a model's agenda into a :class:`SpecReview`.

    Accepts an already-decoded dict or raw model text, tolerating a
    Markdown code fence. Every problem is collected and raised together as
    :class:`SpecReviewValidationError` so one repair round can fix all of
    them.

    ``known_refs`` turns on the hallucination filter: items whose refs name
    nothing in it are dropped (and said so in :attr:`SpecReview.dropped`)
    **after** the bounds are checked, so the repair prompt describes the
    answer the model gave and the filter is free to leave the agenda
    shorter than :data:`MIN_ITEMS` -- or empty, which is a legitimate
    "nothing needs a meeting". Omitting ``known_refs`` runs no filter at
    all, which is not the same as passing an empty set (that drops every
    item naming anything).
    """
    if isinstance(raw, str):
        try:
            data = json.loads(_strip_code_fence(raw))
        except json.JSONDecodeError as exc:
            raise SpecReviewValidationError(
                [f"response is not valid JSON: {exc}"]
            ) from exc
    else:
        data = raw

    if not isinstance(data, dict):
        raise SpecReviewValidationError(
            [f"expected a JSON object, got {type(data).__name__}"]
        )

    errors: list[str] = []
    title = _text(data.get("title"), "title", TITLE_MAX_CHARS, errors)
    summary = _text(data.get("summary"), "summary", SUMMARY_MAX_CHARS, errors)

    items_raw = data.get("items")
    items: list[AgendaItem] = []
    if not isinstance(items_raw, list):
        errors.append(
            f'"items" must be a list of agenda items, '
            f"got {type(items_raw).__name__}"
        )
    else:
        if len(items_raw) < MIN_ITEMS:
            errors.append(
                f"the agenda has {len(items_raw)} item(s); at least "
                f"{MIN_ITEMS} is required (return the run's open questions, "
                f"and none if there are none)"
            )
        if len(items_raw) > MAX_ITEMS:
            errors.append(
                f"the agenda has {len(items_raw)} items, more than "
                f"{MAX_ITEMS}; merge or drop the least important"
            )
        for index, entry in enumerate(items_raw):
            item = _parse_item(entry, index, errors)
            if item is not None:
                items.append(item)

    total = sum(item.minutes for item in items)
    if isinstance(items_raw, list) and items_raw:
        if total < MIN_TOTAL_MINUTES:
            errors.append(
                f"the agenda totals {total} minutes, under the "
                f"{MIN_TOTAL_MINUTES} minute minimum"
            )
        if total > MAX_TOTAL_MINUTES:
            errors.append(
                f"the agenda totals {total} minutes, over the "
                f"{MAX_TOTAL_MINUTES} minute maximum; this is a meeting, "
                f"not a workshop"
            )

    decisions_raw = data.get("decisions_needed")
    decisions: list[str] = []
    if decisions_raw is None:
        decisions_raw = []
    if not isinstance(decisions_raw, list):
        errors.append(
            f'"decisions_needed" must be a list of strings, '
            f"got {type(decisions_raw).__name__}"
        )
    else:
        if len(decisions_raw) > MAX_DECISIONS:
            errors.append(
                f"decisions_needed has {len(decisions_raw)} entries, more "
                f"than {MAX_DECISIONS}"
            )
        for index, decision in enumerate(decisions_raw):
            text = _text(
                decision, f"decisions_needed[{index}]", WHY_MAX_CHARS, errors
            )
            if text:
                decisions.append(text)

    prepared_raw = data.get("prepared_from")
    prepared: list[str] = []
    if prepared_raw is None:
        prepared_raw = []
    if not isinstance(prepared_raw, list):
        errors.append(
            f'"prepared_from" must be a list naming which stages the '
            f"agenda came from, got {type(prepared_raw).__name__}"
        )
    else:
        for index, source in enumerate(prepared_raw):
            if not isinstance(source, str) or source.strip() not in SOURCES:
                errors.append(
                    f"prepared_from[{index}] is {source!r}; allowed: "
                    f"{list(SOURCES)}"
                )
                continue
            prepared.append(source.strip())

    if errors:
        raise SpecReviewValidationError(errors)

    kept = tuple(items)
    dropped: tuple[str, ...] = ()
    if known_refs is not None:
        kept, dropped = _filter_refs(kept, frozenset(known_refs))

    return SpecReview(
        title=title,
        summary=summary,
        items=kept,
        decisions_needed=tuple(dict.fromkeys(decisions)),
        prepared_from=tuple(dict.fromkeys(prepared)),
        dropped=dropped,
    )

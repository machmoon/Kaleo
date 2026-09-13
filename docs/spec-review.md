# Spec review: the run books a meeting instead of printing a wall of text

A run ends with things a machine cannot decide — a blocker the reviewer raised,
three nets the router left as ratsnest, a kernel clause that failed by 0.4 mm, a
part nobody could source. The old answer was to print all of it and hope
somebody read it. This feature is the other answer: the run proposes a **short
agenda**, and the agenda becomes half an hour on the calendar with a Meet link
and the people who can answer it in the room.

Four pieces, each with its own honesty rule:

| Piece | Where |
|---|---|
| The IR — validated model output, bounded | `engine/silkscreen/specreview.py` |
| The model call and its one repair round | `engine/silkscreen/agents/specreview.py` |
| Scheduling — agenda in, calendar hold out | `googleapps/specreview.py` |
| The `spec_review` deliver destination | `service/deliver.py` |
| The agenda alongside the findings | `service/steps.py::_review` |
| Structured / Prose toggle and the Send panel | `app/src/pages/kaleo/components/RunOptions.tsx`, `DeliverPanel.tsx` |

---

## 1. The IR (`silkscreen.specreview`)

`parse_spec_review(raw, *, known_refs=None)` takes raw model output (a fenced
JSON block is tolerated) and returns a `SpecReview`. It follows the
`silkscreen.netlist` convention throughout: **every** failure is collected into
one `SpecReviewValidationError`, whose `errors` list goes back to the model as a
single repair prompt, rather than one round trip per mistake.

```python
@dataclass(frozen=True)
class AgendaItem:
    topic: str; why: str; minutes: int; blocking: bool
    refs: tuple[str, ...] = ()          # part refs / net names; empty is legitimate

@dataclass(frozen=True)
class SpecReview:
    title: str
    summary: str
    items: tuple[AgendaItem, ...] = ()
    decisions_needed: tuple[str, ...] = ()
    prepared_from: tuple[str, ...] = ()  # subset of SOURCES
    dropped: tuple[str, ...] = ()        # one message per filtered item
    def total_minutes(self) -> int: ...
    @property
    def blocking(self) -> tuple[AgendaItem, ...]: ...
    @property
    def needs_meeting(self) -> bool: ...  # = bool(self.blocking)
    def as_dict(self) -> dict: ...
```

### The bounds are the feature, not decoration

| Bound | Value | Why |
|---|---|---|
| `SUMMARY_MAX_CHARS` | 400 | The anti-wall-of-text rule. A summary that grows into one has quietly turned back into the thing this replaces. |
| `TITLE_MAX_CHARS` / `TOPIC_MAX_CHARS` | 120 | |
| `WHY_MAX_CHARS` | 400 | |
| `MIN_ITEMS` … `MAX_ITEMS` | 1 … 8 | Nobody decides fifteen things in a meeting; an agenda that long is a report wearing an agenda's hat. |
| `MAX_ITEM_MINUTES` | 60 | A single item longer than the whole meeting is a mis-typed number, worth saying so before the total is summed. |
| `MIN_TOTAL_MINUTES` … `MAX_TOTAL_MINUTES` | 15 … 60 | Below the floor there is nothing worth booking a room for; above the ceiling the model is proposing a workshop, and nobody will accept it. |
| `MAX_DECISIONS` | 8 | |
| `SOURCES` | `("review", "route", "kernel", "sourcing")` | Frozen vocabulary: the four run stages that produce something a human might have to judge. |

### Two rules that make it honest

**The hallucination filter.** An item whose `refs` name nothing the board
contains is **dropped**, not reported — the `agents/review.py` filter applied
again, turned on by passing `known_refs`. An agenda item pointing at `R9` on a
board with eight resistors sends a person hunting for something that was never
there. Each drop is recorded in `SpecReview.dropped`, because a filter that
drops without saying so is indistinguishable from a model that said nothing.
The bounds above are checked against what the model *proposed*, before the
filter runs, so the repair prompt talks about the answer the model actually
gave; the filter may then legitimately leave fewer items than the floor, or
none at all.

**Three states, three representations.** `SpecReviewResult` keeps them apart:

- `review is None` with a warning — the model never gave a usable answer.
- `SpecReview` with empty `items` — it answered, and there is nothing to
  discuss. **A legitimate result**, not a gap. Nothing invents an agenda to
  fill a slot.
- No `SpecReviewResult` at all — the stage never ran.

`SpecReviewResult.needs_meeting` is false both when there is nothing to discuss
and when the model failed: never book a meeting off a failure. `ok` is what
separates the two.

---

## 2. The model call (`silkscreen.agents.specreview`)

`propose_spec_review(model, *, board_summary, findings, unrouted, clauses,
sourcing, known_refs=None, max_repairs=1, on_event=None)` — shaped exactly like
`agents/sourcing.py`.

`evidence_block(...)` renders what the run actually produced into the prompt and
returns three things: the text, the sources that contributed (a subset of
`SOURCES`, in that order), and every ref or net name the evidence mentioned.
That last one is the default `known_refs`, so **an item may only be about
something the model was actually shown**. The four sources are duck-typed on
purpose — a review `Finding`, an enclosure kernel `Clause` and a
`SourcingEntry` come from three layers that do not import each other, and this
module refuses to be the place that joins them. Failed clauses report their
signed margin in millimetres; each piece of evidence is clipped to 300
characters and each source to 12 lines (a board with forty unrouted nets needs
a meeting about routing, not forty agenda items — and the count is stated
either way).

`SPECREVIEW_MARKER = "SPEC-REVIEW-AGENDA v1"` appears verbatim in every prompt
so a `ScriptedModel.by_marker` can key on it.

The loop: one call, plus at most `max_repairs` more when
`parse_spec_review` rejects the answer, each time sending the batched errors
back as one repair prompt. When the budget is spent it **gives up loudly** —
`review=None` and one warning naming the failure. A half-parsed agenda would
book a meeting about the wrong thing, which is worse than booking none. A
`ModelError` is deliberately **not** wrapped (the `propose_circuit`
convention): an outage is a different condition from a bad answer, and callers
route them differently.

**With no evidence at all the model is not called.** The result is
`_nothing_to_discuss(...)` — a `SpecReview` with no items whose summary says the
run flagged nothing. Asking a model to summarise an empty list is how an
invented agenda gets born.

`on_event` emits `specreview.round` per rejected answer, one
`specreview.dropped` per filtered item, and a final `specreview.agenda`
carrying counts and minutes — never the model's text.

---

## 3. The agenda alongside the findings (`service/steps.py`)

The overlay's `POST /steps/<id>/review` gains an agenda when the caller asks for
one. Two ways, because there are two callers:

- `{"spec_review": true}` — says it outright.
- `{"summary": "structured"}` — the desktop's Structured toggle, whose whole
  meaning is "give me an agenda rather than prose".

Default off: the agenda is a model call, and the service does not spend one
nobody asked for. `'spec_review' must be a boolean` and `'summary' must be
'prose' or 'structured'` are 400s.

The response body gains `"spec_review"`, the `SpecReviewResult.as_dict()`. The
findings are the product of that step and an agenda is a convenience on top, so
a failure becomes `"spec_review": null` plus a `warnings` entry naming the
exception — and a null block is not silence, it travels with the reason, and
`needs_meeting` stays false so nothing downstream books a meeting off an error.
The result is also kept on the session (`Session.spec_review`), which is where
the deliver step reads it from.

---

## 4. The `spec_review` deliver destination (`service/deliver.py`)

`POST /steps/<session_id>/deliver`, the same route the Chat/Gmail/Calendar
destinations use. Every destination succeeds or fails on its own inside one
200.

### Request

```jsonc
{
  "spec_review": true,                      // boolean, required to book one
  "attendees": ["ada@example.com"],         // list, or a comma-separated string
  "when": "2026-09-08T15:00:00Z"            // RFC 3339 with an offset, or null
}
```

`when` is optional; `null` (or omitted) books the default lead time ahead of
now. It may be combined with the other destinations in the same request
(`"chat"`, `"email"`, `"schedule"`).

### Response

```jsonc
{
  "session": "<session id>",
  "spec_review": {
    "ok": true,
    "html_link": "https://www.google.com/calendar/event?eid=…",
    "meet_uri": "https://meet.google.com/abc-defg-hij",
    "minutes": 35,
    "items": 4,
    "blocking": 2
  }
}
```

Three answers, never a fourth: booked, deliberately skipped with the reason, or
failed with the error.

```jsonc
{"spec_review": {"ok": false, "skipped_reason": "the review step has not run, so what the meeting would decide is not known — nothing scheduled"}}
{"spec_review": {"ok": false, "skipped_reason": "the agenda has no blocking item — nothing needs a meeting, so nothing was scheduled"}}
{"spec_review": {"ok": false, "error": "…"}}
```

### Every refusal

Validated **before** the session is read, let alone a request sent — all 400s
with the message shown:

| Condition | Message |
|---|---|
| No destination named | `nothing to deliver: set 'chat', give 'email' addresses, or set 'schedule' or 'spec_review' with 'attendees'` |
| `spec_review` without attendees | `'spec_review' needs at least one address in 'attendees'` |
| Attendees with neither event flag | `'attendees' only means something with 'schedule' or 'spec_review'` |
| `when` without `spec_review` | `'when' only means something with 'spec_review'` |
| `when` not RFC 3339 | `'when' is not an RFC 3339 timestamp: …` |
| `when` with no offset | `'when' needs a timezone offset: … (append 'Z' for UTC, or '+02:00')` |
| `when` in the past | `'when' is in the past: …` — a meeting in the past is a typo, and booking it while reporting success would hide the typo |
| `spec_review` not a boolean | `'spec_review' must be a boolean` |
| A bad address | `googleapps.addresses.validate_addresses`' own message, naming every bad address at once |
| `googleapps` not installed | `delivery is unavailable: the googleapps package is not installed beside the service` |

Route-level: an unknown session is a **404**, a session that is not `routed` yet
is a **409** (the board file exists only after the route step).

Deeper down, `googleapps.specreview.schedule_spec_review` refuses again — a
`GoogleError("bad_request", "a spec review needs at least one attendee")` and a
`GoogleError("no_blockers", "the agenda has no blocking item, so nothing needs
a meeting")`. The decision not to hold a meeting belongs to the caller; this is
the backstop for a caller that forgot to check.

### Where the agenda comes from

If the session carries a `SpecReview` (the review step was asked for one),
`agenda_from_spec_review` uses it. Otherwise `agenda_from_result` builds a
**deterministic** agenda with no model call: each blocking finding is a blocking
item, and a ratsnest is one more item (an unrouted net is a decision — finish
it, or ship it that way — not a detail). No item is invented: a reviewed board
with no blockers and no ratsnest yields an agenda with nothing blocking, which
the caller then declines to book.

The engine's own `total_minutes` wins over re-adding the items — the panel that
showed the engineer "35 min" read that number, and the hold has to be the
meeting they agreed to — clamped to 15–60. A value outside the range is
clamped, never rejected: a meeting five minutes too long is not a reason to
refuse to book it.

### What lands in the calendar

`sendUpdates=all` (without it the event is created and no attendee is told), a
Meet link via `conferenceData.createRequest` with
`conferenceSolutionKey.type = "hangoutsMeet"`, attendees validated before any
network call, and a description that is the agenda — summary, numbered items
with minutes and a `[blocking]` flag, each item's `why` and `refs`, the
decisions needed, **every unrouted net named verbatim**, and what it was
prepared from. The whole description goes through `html.escape`, because
Calendar renders a subset of HTML in it (the same rule `chat.py` applies to
`textParagraph`).

### On the CLI

```bash
python -m googleapps run "a 3.3V LDO board" --spec-review --attendee ada@example.com
```

`--spec-review` sits alongside `--schedule` and shares `--attendee`.
`--spec-review` with no `--attendee` is a `ConfigError`; `--attendee` without
either flag is too. With `--no-review` it prints `review was skipped
(--no-review) — no spec review was booked`; with nothing blocking, `nothing on
the agenda is blocking — no spec review was booked`. Otherwise it prints the
booked duration, the blocking count, the event link and the Meet URI.

---

## 5. The desktop half

**RunOptions — Structured / Prose.** How a finished run reports itself:

- **Structured** — "An agenda: what is still open, why each item blocks, how
  long it needs. Bookable as a spec review from the Send panel."
- **Prose** — "A written summary of the run, the way it reads today."

The choice is remembered (`kaleo.summaryMode`, default `prose`) and folded into
the request as `{"summary": "structured" | "prose"}` by
`summaryFields(mode)`, which is what `service/steps.py::_wants_agenda` reads.

**DeliverPanel — "Book a spec review".** The panel shows the agenda and its
total minutes *before* it sends anything, so the engineer approves what will
actually go out. It never assembles an agenda of its own: an agenda invented in
the app would be this app's opinion presented as the engine's. `specReviewOffer`
supplies the note when there is nothing to show — "The review step has not run,
so there is no agenda yet" or "The review ran but proposed no agenda, so there
is nothing to put in a meeting" or "No blockers — nothing needs a meeting" — and
the button stays live either way, because the engine is the authority on what it
will book and answers with its own `skipped_reason`. One request in flight at a
time: every button is a real send.

The button posts `{"spec_review": true, "attendees": [...], "when": null}`.

---

## Two skip reasons, and why they are not errors

1. **The review has not run.** Whether there is anything to discuss is *not
   known*, and guessing is the failure this rule exists to prevent. The same
   sentence the existing `--schedule` path uses ("the review step has not run")
   rather than an invented verdict.
2. **Nothing is blocking.** The agenda ran, and it says the board does not need
   a meeting. That is a result — the best possible one — and reporting it as a
   skip rather than booking an empty half hour of four people's day is the
   whole point of `needs_meeting` being derived from the blocking items instead
   of from the truthiness of a list.

---

## Tests

- `engine/tests/test_specreview.py` — the IR's bounds, the batched validation,
  the ref filter and the three states.
- `googleapps/tests/test_workspace_specreview.py` — the agenda sources, the
  event body, `parse_when`, the refusals.
- `service/tests/test_deliver_specreview.py` — the destination end to end
  against a recorded transport.

Everything offline, against a recorded transport and a scripted model. Like the
rest of `googleapps/`, the Calendar path has **never been run against live
Google APIs**; the offline tests pin request construction, and the first live
run is the real test.

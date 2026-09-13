"""Adversarially review a circuit against the datasheets it was drawn from.

Structural validity is not correctness. A netlist can pass every check in
:mod:`silkscreen.netlist` and still drive an input pin, wire a regulator's
feedback divider to the wrong node, or put a ceramic cap where the part needs
tantalum. That gap -- between "connected" and "connected *correctly*" -- is what
no EDA tool checks and what this module exists for.

The reviewer is prompted to *refute* the design. An agent asked "is this
correct?" says yes.

Three things happen to what it answers, in this order:

**Specialisation.** One prompt asks the model to review the circuit as three
named specialists -- power, signal, manufacturability -- and to label every
finding with the specialist that raised it. That is one model call, not three;
the measurement behind that choice is in ``docs/critic-split.md``.

**Premise checking.** Every finding must state, in structured form, the
premises about the circuit it rests on: a part's value, a terminal's net, a
terminal being floating, a part's pin count. Each premise is checked against
the validated :class:`~silkscreen.netlist.CircuitSpec`, which is the ground
truth the board is actually built from. A finding whose premise the spec
refutes is dropped and the refutation is reported. This is
:mod:`meetings.intent`'s quote filter applied to a circuit instead of a
transcript: there the model must supply a verbatim quote and the code checks it
against the source; here it must supply a premise and the code checks it
against the source. In both cases the model is made to say something that can
be *wrong*, rather than only something that can be *unconvincing*.

**Merging.** Two specialists arguing the same defect must reach the engineer
once, not twice, and the merge must be visible. The shape is taken from tools
that have merged findings across independent analysers for years -- semgrep's
``dedup_and_sort``, sarif-sdk's ``WhatComparer``, golangci-lint's suppression
reporting -- each cited at the function that borrows from it.

A fourth thing happens only when asked for. ``refute=True`` spends one more
call in which every surviving finding must defend itself, which is
``audit/effort.py``'s ``refute_rounds`` at ``deep``, batched. Who asks is the
effort level: it is :attr:`~silkscreen.agents.effort.EffortProfile.refute`,
on at ``thorough`` and off below it, because it is a model call nobody pressed
for and because, measured, it throws away true findings along with false
ones -- a cost worth paying when someone asked for the careful answer.

Everything this module produces is an *argument*, never a measurement. The
:class:`Domain` on a finding says which specialist raised it; it never says
how the claim was established, so nothing here can enter a result looking like
a fact the way ``audit/findings.py``'s ``Origin.PROVEN`` does. That distinction
is why a simulation verdict still does not belong on
``PipelineResult.findings``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import StrEnum

from ..netlist import CircuitSpec, normalise_pin_number
from .datasheet import PartFacts
from .model import Model, ModelError, parse_json

__all__ = [
    "Finding",
    "Severity",
    "Domain",
    "SPECIALISTS",
    "Check",
    "CheckStatus",
    "normalise_value",
    "ReviewError",
    "ReviewStatus",
    "ReviewReport",
    "ReviewOutcome",
    "SpecIndex",
    "review_circuit",
    "run_review",
    "REVIEW_PROMPT",
    "REFUTE_PROMPT",
]


class ReviewError(ModelError):
    """The critic was asked and its answer could not be read.

    Deliberately *not* an empty finding list. "The critic reviewed this board
    and found nothing" and "the critic's answer was unreadable" are the same
    bytes to every consumer downstream unless they are different outcomes
    here, and the first of those two claims is one no model failure has earned.
    ``audit/report.py`` already states the rule for the other reviewer -- an
    empty finding list must not read as a clean board -- and this is the engine
    side of it.
    """


class Severity(StrEnum):
    #: The board will not work as built.
    BLOCKER = "blocker"
    #: It may work, but violates a datasheet recommendation or good practice.
    MARGINAL = "marginal"
    #: Correct, but worth knowing.
    NOTE = "note"


#: Most severe first. Shared by the sort and by the merge, which keeps the
#: harsher of two severities when two specialists disagree about one claim.
SEVERITY_ORDER = {Severity.BLOCKER: 0, Severity.MARGINAL: 1, Severity.NOTE: 2}


class Domain(StrEnum):
    """Which specialist raised a finding.

    Not provenance in the ``audit.findings.Origin`` sense: every value here
    means "a model argued this", and none of them means "something measured
    this". The split exists because a single undifferentiated "find what is
    wrong" prompt reliably finds two or three things and stops -- the same
    observation ``audit/judgment.py``'s ``per_part_focus`` is built on, applied
    to domains instead of to parts.
    """

    POWER = "power"
    SIGNAL = "signal"
    MANUFACTURABILITY = "manufacturability"
    #: The answer named no specialist, or named one this does not know.
    GENERAL = "general"


#: The three the prompt actually asks for. ``GENERAL`` is the fallback for an
#: answer that skipped the label, never something the model is asked to emit.
SPECIALISTS: tuple[Domain, ...] = (
    Domain.POWER,
    Domain.SIGNAL,
    Domain.MANUFACTURABILITY,
)


class ReviewStatus(StrEnum):
    #: The critic answered and its answer was read. ``findings`` is complete,
    #: and an empty ``findings`` here -- and only here -- means it found
    #: nothing.
    OK = "ok"
    #: The critic was asked and its answer could not be read. Nothing is known
    #: about the board's correctness.
    FAILED = "failed"
    #: The critic was never asked. Also nothing known, for a different reason.
    SKIPPED = "skipped"


class CheckStatus(StrEnum):
    """What the spec had to say about one premise a finding rests on."""

    #: The spec agrees. The finding is arguing about the real circuit.
    CONFIRMED = "confirmed"
    #: The spec disagrees. The finding is arguing about a circuit that is not
    #: the one being built, so it is dropped.
    REFUTED = "refuted"
    #: The premise is not one this can decide. Recorded, never used to drop.
    UNCHECKABLE = "uncheckable"


@dataclass(frozen=True)
class Check:
    """One premise, and what the validated spec said about it."""

    status: CheckStatus
    detail: str

    def __str__(self) -> str:
        return f"{self.status.value}: {self.detail}"


@dataclass(frozen=True)
class Finding:
    severity: Severity
    title: str
    detail: str
    parts: tuple[str, ...] = ()
    citation: str = ""
    suggested_fix: str = ""
    #: Net names the finding is about, filtered to nets the circuit has, the
    #: same way ``parts`` is. ``audit/judgment.py`` already keeps both.
    nets: tuple[str, ...] = ()
    #: Which specialist raised it.
    domain: Domain = Domain.GENERAL
    #: Every specialist that raised this same claim, most severe first. One
    #: entry is the normal case; two or three means independent agreement, and
    #: it is the only confidence signal here that is not the model's own
    #: self-report.
    agreed_by: tuple[Domain, ...] = ()
    #: One line per premise checked against the spec. A finding with no
    #: checkable premise says so here rather than saying nothing.
    checks: tuple[Check, ...] = ()

    def __str__(self) -> str:
        where = f" [{', '.join(self.parts)}]" if self.parts else ""
        cite = f"  ({self.citation})" if self.citation else ""
        return f"{self.severity.value.upper()}{where}: {self.title}{cite}"

    @property
    def confirmed_premises(self) -> tuple[str, ...]:
        return tuple(
            c.detail for c in self.checks if c.status is CheckStatus.CONFIRMED
        )


@dataclass(frozen=True)
class ReviewOutcome:
    """Everything one critic pass produced, including what it threw away.

    Separate from :class:`ReviewReport` because a report also carries the
    *status* of the pass, which only the stage knows (it owns the failure and
    the never-ran cases). This is the pure result of reading one answer.
    """

    findings: tuple[Finding, ...] = ()
    #: One line per entry the filter removed.
    dropped: tuple[str, ...] = ()
    #: One line per pair of findings collapsed into one.
    merged: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReviewReport:
    """What the critic pass produced, including the fact that it produced
    nothing usable.

    Carried on :class:`~silkscreen.agents.pipeline.PipelineResult` beside the
    findings themselves so every consumer -- the CLI, the Slack card, the
    Gmail body, the Calendar decision, the desktop receipt -- can tell the
    three outcomes apart without inspecting a list length.
    """

    status: ReviewStatus = ReviewStatus.SKIPPED
    findings: tuple[Finding, ...] = ()
    #: Why the answer could not be read, when it could not. Empty otherwise.
    detail: str = ""
    #: One line per finding the filter removed: a finding that named only
    #: parts the circuit does not contain, a premise the spec refuted, or an
    #: entry too malformed to read. A filter that drops silently is
    #: indistinguishable from a critic that said nothing, which is the same
    #: rule `SpecReview.dropped` exists for.
    dropped: tuple[str, ...] = ()
    #: One line per merge. A merge hides a finding exactly as a drop does --
    #: the reader sees one row where the critic wrote two -- so it is reported
    #: on the same terms.
    merged: tuple[str, ...] = ()

    @property
    def ran(self) -> bool:
        """Was the critic asked at all?"""
        return self.status is not ReviewStatus.SKIPPED

    @property
    def ok(self) -> bool:
        """Did it answer readably? Only then does an empty list mean clean."""
        return self.status is ReviewStatus.OK

    @property
    def failed(self) -> bool:
        return self.status is ReviewStatus.FAILED

    @property
    def blockers(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.BLOCKER)

    def by_domain(self) -> dict[Domain, tuple[Finding, ...]]:
        """Findings grouped by the specialist that raised them."""
        out: dict[Domain, list[Finding]] = {}
        for finding in self.findings:
            out.setdefault(finding.domain, []).append(finding)
        return {domain: tuple(items) for domain, items in out.items()}

    def note(self) -> str:
        """One sentence about the review, safe to put in front of a human.

        The failed and skipped sentences deliberately say what is *not known*
        rather than what was found, because nothing was found -- the critic
        never delivered a verdict to summarise.
        """
        if self.status is ReviewStatus.SKIPPED:
            return "the review did not run, so nothing is known about this board"
        if self.status is ReviewStatus.FAILED:
            reason = f" ({self.detail})" if self.detail else ""
            return (
                "the review failed to produce a readable answer"
                f"{reason}, so nothing is known about this board"
            )
        if not self.findings:
            return "the review ran and found nothing"
        return (
            f"the review found {len(self.findings)} finding(s), "
            f"{len(self.blockers)} blocker(s)"
        )

    def as_dict(self) -> dict[str, object]:
        """The JSON shape a service surface puts on the wire.

        ``dropped`` and ``merged`` are here because a client reading only this
        dict could otherwise not tell a quiet filter from a quiet critic,
        which is the whole thing the two fields exist to prevent: three
        findings that arrived and two that were shown look exactly like two
        findings that arrived, and the difference is the one a person needs
        when the critic seems to have missed something obvious.

        They are lists rather than counts for the same reason
        ``SpecReview.dropped`` is: a number says work was hidden and a line
        says which work, and only the second sends anyone anywhere. Empty
        lists are the normal case and are still emitted, since an absent key
        reads as "this surface does not know", which is a different claim.
        """
        return {
            "status": self.status.value,
            "ran": self.ran,
            "detail": self.detail,
            "note": self.note(),
            "dropped": list(self.dropped),
            "merged": list(self.merged),
        }


# --------------------------------------------------------------------------
# The spec index: what a claim can be checked against.
# --------------------------------------------------------------------------

#: SI/EE multipliers, case-sensitive first because ``M`` is mega and ``m`` is
#: milli and a resistor value is the one place that difference is nine orders
#: of magnitude. This is SPICE's own rule, which ``spice/deck.py`` already
#: follows for the same reason.
_MULTIPLIER = {
    "f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6,
    "m": 1e-3, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "R": 1.0,
    "": 1.0,
}

#: ``4k7``/``1R2``/``2u2`` -- the multiplier sits where the decimal point goes.
_VALUE_RE = re.compile(
    r"^([0-9]*\.?[0-9]+)\s*"          # magnitude
    r"([fpnuµμmkKMGTR]?)"              # multiplier, or nothing
    r"([0-9]*)\s*"                     # 4k7's trailing digits
    r"([a-zA-ZΩ]*)$"                   # unit, or nothing
)

def normalise_value(text: str) -> str:
    """One spelling per component value, so two spellings can be compared.

    ``10uF``, ``10 µF``, ``10u`` and ``10.0 uF`` are one value; ``10nF`` is
    not. A value this cannot parse -- ``"Red LED"``, ``"AMS1117-3.3"`` --
    comes back as its own lowercased text, so comparing two of those is a
    plain string comparison and never a false claim of disagreement.

    **The unit is discarded, only the magnitude survives.** Two values are
    only ever compared here when they are two claims about *the same named
    passive*, whose type already fixes what the unit must be, so ``10u`` and
    ``10uF`` have to agree -- and the one direction this can be wrong in
    (calling ``10uF`` and ``10uH`` equal) makes the filter keep a finding it
    might have dropped, which is the safe way round for a filter that can
    throw away a true blocker.

    ``normalise_pin_number`` in :mod:`silkscreen.netlist` exists for exactly
    this reason on the other side of the IR, and the argument is the same: a
    check written against one spelling silently passes on another.
    """
    raw = str(text).strip()
    match = _VALUE_RE.match(raw)
    if match is None:
        return raw.lower()
    magnitude, mult, trailing, _unit = match.groups()
    if trailing and not mult:
        return raw.lower()
    number = float(f"{magnitude}.{trailing}") if trailing else float(magnitude)
    scale = _MULTIPLIER.get(mult, _MULTIPLIER.get(mult.lower(), 1.0))
    # 12 significant figures: enough that 4k7 and 4700 agree, few enough that
    # binary float noise never makes two spellings of one value differ.
    return f"{float(f'{number * scale:.12g}'):g}"


@dataclass(frozen=True)
class SpecIndex:
    """The validated circuit, in the shapes a premise check needs.

    Built from the :class:`~silkscreen.netlist.CircuitSpec` and nothing else.
    The spec is what the board is actually built from, so it is the only thing
    in reach at this stage that can call a claim wrong.
    """

    parts: frozenset[str]
    nets: frozenset[str]
    #: ``"U1.VIN" -> "VIN"``, for every terminal that is on a net.
    net_of_terminal: dict[str, str]
    #: Passive name -> its normalised value. Devices have no value.
    value_of: dict[str, str]
    #: Device name -> how many pins it declares.
    pin_count: dict[str, int]
    #: Device name -> the pin names it declares.
    pins_of: dict[str, frozenset[str]]

    @classmethod
    def of(cls, spec: CircuitSpec) -> SpecIndex:
        net_of_terminal: dict[str, str] = {}
        for conn in spec.connections:
            for endpoint in conn.endpoints:
                part, _, pin = endpoint.rpartition(".")
                net_of_terminal[f"{part}.{normalise_pin_number(pin)}"] = conn.net
        return cls(
            parts=frozenset(
                [d.name for d in spec.devices] + [p.name for p in spec.passives]
            ),
            nets=frozenset(c.net for c in spec.connections),
            net_of_terminal=net_of_terminal,
            value_of={p.name: normalise_value(p.value) for p in spec.passives},
            pin_count={d.name: len(d.pins) for d in spec.devices},
            pins_of={d.name: frozenset(d.pins) for d in spec.devices},
        )

    def terminal(self, text: str) -> str | None:
        """``"U1.VIN"`` as this index spells it, or None if it is not one.

        Pin numbers are normalised the way the IR normalises them, so a claim
        written ``C_in.01`` and a spec written ``C_in.1`` are the same pin.
        """
        part, dot, pin = str(text).strip().rpartition(".")
        if not dot or part not in self.parts:
            return None
        pin = normalise_pin_number(pin)
        if part in self.pins_of and pin not in self.pins_of[part]:
            return None
        if part in self.value_of and pin not in ("1", "2"):
            return None
        return f"{part}.{pin}"


# --------------------------------------------------------------------------
# Premise checking.
# --------------------------------------------------------------------------

def _check_claim(index: SpecIndex, claim: object) -> Check:
    """Decide one stated premise against the validated spec.

    Four kinds, chosen because each is decidable from the IR with no
    judgement at all -- the whole point is that this half of the filter
    cannot itself be wrong about the circuit.
    """
    if not isinstance(claim, dict):
        return Check(
            CheckStatus.UNCHECKABLE,
            f"a premise that was not an object ({type(claim).__name__})",
        )
    kind = str(claim.get("kind", "")).strip().lower()

    if kind == "value":
        part = str(claim.get("part", "")).strip()
        want = normalise_value(str(claim.get("value", "")))
        if part not in index.value_of:
            return Check(
                CheckStatus.UNCHECKABLE,
                f"{part!r} has no value in the spec (it is not a passive)",
            )
        have = index.value_of[part]
        if have == want:
            return Check(CheckStatus.CONFIRMED, f"{part} is {claim['value']}")
        return Check(
            CheckStatus.REFUTED,
            f"the finding assumes {part} is {claim.get('value')!r}, "
            f"but the circuit gives it {have}",
        )

    if kind == "connected":
        terminal = index.terminal(claim.get("terminal", ""))
        want = str(claim.get("net", "")).strip()
        if terminal is None:
            return Check(
                CheckStatus.UNCHECKABLE,
                f"{claim.get('terminal')!r} is not a terminal of this circuit",
            )
        have = index.net_of_terminal.get(terminal)
        if have == want:
            return Check(CheckStatus.CONFIRMED, f"{terminal} is on {want}")
        return Check(
            CheckStatus.REFUTED,
            f"the finding assumes {terminal} is on net {want!r}, but the "
            + (
                f"circuit puts it on {have!r}"
                if have
                else "circuit leaves it unconnected"
            ),
        )

    if kind == "floating":
        terminal = index.terminal(claim.get("terminal", ""))
        if terminal is None:
            return Check(
                CheckStatus.UNCHECKABLE,
                f"{claim.get('terminal')!r} is not a terminal of this circuit",
            )
        have = index.net_of_terminal.get(terminal)
        if have is None:
            return Check(CheckStatus.CONFIRMED, f"{terminal} is on no net")
        return Check(
            CheckStatus.REFUTED,
            f"the finding assumes {terminal} is floating, but the circuit "
            f"puts it on net {have!r}",
        )

    if kind == "pin_count":
        part = str(claim.get("part", "")).strip()
        if part not in index.pin_count:
            return Check(
                CheckStatus.UNCHECKABLE,
                f"{part!r} is not a device of this circuit",
            )
        try:
            want = int(claim.get("pins"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return Check(
                CheckStatus.UNCHECKABLE,
                f"{claim.get('pins')!r} is not a pin count",
            )
        have = index.pin_count[part]
        if have == want:
            return Check(CheckStatus.CONFIRMED, f"{part} has {have} pins")
        return Check(
            CheckStatus.REFUTED,
            f"the finding assumes {part} has {want} pins, but the circuit "
            f"declares {have}",
        )

    return Check(
        CheckStatus.UNCHECKABLE, f"a premise of unknown kind {kind!r}"
    )


# --------------------------------------------------------------------------
# The merge.
#
# Three decisions here are lifted from tools that have been merging findings
# from independent analysers for years, and each is cited where it is used:
#
#   * semgrep, ``src/reporting/Core_json_output.ml``, ``dedup_and_sort`` --
#     sort, then keep the first of each key, then sort again; and keep
#     *conflicting* attributes out of the key, resolving them with an explicit
#     preference function (``should_report_instead``) instead.
#   * microsoft/sarif-sdk, ``src/Sarif/Baseline/V2/WhatComparer.cs``,
#     ``MatchesWhat`` -- "true if ANY 'What' property matches", sharpened by a
#     hard non-match when both sides carry fingerprints and none agree; and
#     ``ResultMatchingBaselinerFactory``'s rule that exact matchers run before
#     heuristic ones.
#   * golangci-lint, ``pkg/result/processors/max_same_issues.go``, ``Finish``
#     -- a suppressed finding is counted and named on the way out, never
#     silently dropped.
#
# One thing deliberately NOT taken: inspect_ai's ``majority`` reducer
# (``src/inspect_ai/scorer/_reducer/reducer.py``), which discards anything a
# strict majority of the panel did not vote for. That is the right rule for N
# judges answering ONE question and the wrong one here, because these three
# specialists are answering three different questions: only the power critic
# is looking for an ESR problem, so requiring two of three to see it would
# throw away every finding the split exists to buy. What does carry over is
# that panel's other discipline -- ``_with_panel_metadata`` records who voted
# for what -- which is what ``Finding.agreed_by`` is.
# --------------------------------------------------------------------------

#: Words that carry no identity. Dropped from a title before two titles are
#: compared, so "Output capacitor is ceramic" and "The output capacitor is a
#: ceramic" are one claim.
_STOPWORDS = frozenset((
    'a', 'an', 'and', 'are', 'as', 'at', 'be', 'been', 'but', 'by', 'for',
    'from', 'has', 'have', 'in', 'is', 'it', 'its', 'no', 'not', 'of', 'on',
    'or', 'that', 'the', 'there', 'this', 'to', 'too', 'was', 'were', 'will',
    'with', 'without'
))


def _signature(title: str) -> frozenset[str]:
    """The identity of a claim, as a set of content words.

    Two specialists never phrase a title identically, so the exact-string key a
    single-tool deduper can afford merges nothing here. Content rather than
    position is the same choice GitHub code scanning makes in
    ``github/codeql-action``, ``src/fingerprints.ts``: its
    ``primaryLocationLineHash`` hashes the *characters* of the line with
    whitespace skipped, deliberately not the line number, so an edit elsewhere
    in the file does not turn one alert into a new one.
    """
    words = re.findall(r"[a-z0-9_.+]+", title.lower())
    return frozenset(w for w in words if w not in _STOPWORDS and len(w) > 2)


def _location(finding: Finding) -> tuple[frozenset[str], frozenset[str]]:
    """Where a finding is, which is the strict half of its identity."""
    return frozenset(finding.parts), frozenset(finding.nets)


def _same_claim(left: Finding, right: Finding, *, exact: bool) -> bool:
    """Are these two specialists arguing the same defect?

    Location first, prose second -- and the location is always the strict
    half. Semgrep's key is ``(rule, path, start, end, message)``: neither the
    location alone (which merges two genuinely different defects at one place)
    nor the message alone (which merges the same defect found twice in two
    places) is identity, and the same holds for a circuit, where a "location"
    is the set of parts and nets a finding names.

    Two passes, exact before heuristic, which is
    ``ResultMatchingBaselinerFactory``'s stated rule in microsoft/sarif-sdk:
    "Exact matchers run first... These should do no remapping and offer fast
    comparisons to filter out common cases. Heuristic matchers run in order
    after the exact matchers... and catch the long tail."

    * ``exact`` -- same location, same signature. Two critics saying the same
      thing in the same words.
    * heuristic -- same location, signatures that *overlap*. This is
      ``WhatComparer.MatchesWhat``'s "true if any 'What' property matches",
      with its sharpening applied: an unlocated finding has no strict half
      left, so it falls back to requiring the signatures to agree outright
      rather than merely overlap.

    The asymmetry is deliberate. An under-merge shows the reader a
    near-duplicate, which they can see and dismiss; an over-merge deletes a
    real defect, which they cannot. Only one of those is recoverable, so the
    location test never loosens.
    """
    if _location(left) != _location(right):
        return False
    left_sig, right_sig = _signature(left.title), _signature(right.title)
    if exact or not (left.parts or left.nets):
        return left_sig == right_sig
    return bool(left_sig & right_sig)


def _sort_key(finding: Finding) -> tuple:
    """A **total** order over findings, which the merge depends on.

    Semgrep's ``Semgrep_output_utils.compare_match`` carries a comment saying
    exactly why totality matters: findings that compare equal fall through to
    the iteration order of the dedup table, and "autofix applies the first
    edit of an identical span, so an unstable order there picks a different
    fix". The hazard here is the same shape -- ``_merge`` keeps the *first* of
    each group and folds the rest into it, so an unstable order changes which
    wording, fix and citation the engineer is shown.
    """
    return (
        SEVERITY_ORDER[finding.severity],
        finding.domain.value,
        finding.title,
        tuple(sorted(finding.parts)),
        tuple(sorted(finding.nets)),
    )


def _merge(findings: list[Finding]) -> tuple[list[Finding], list[str]]:
    """Collapse one defect raised by several specialists into one finding.

    Sort, keep the first of each group, sort again -- semgrep's
    ``dedup_and_sort`` in order. The first sort is what makes "first" mean
    "most severe", so the survivor is never chosen by the order the model
    happened to emit its findings in; the second is because the merge itself
    can change a severity and so can reorder the list.

    **Severity is not part of the key.** It is resolved afterwards by an
    explicit preference -- the harshest any specialist gave it, never an
    average or a vote. This is semgrep's own correction: it pulled
    ``validation_state`` out of ``core_unique_key`` and added
    ``should_report_instead`` so a confirmed-valid match replaces an
    unconfirmed one with the same key, rather than the two failing to dedup
    because they disagreed. Averaging would be worse than either: a blocker is
    a claim that the board will not work, and a second critic filing the same
    defect as a note has not made the board work.

    Agreement is the confidence signal: ``agreed_by`` names every specialist
    that raised the claim. It is not asked of the model, because a model's
    self-reported confidence is uncalibrated and a second specialist reaching
    the same conclusion independently is not.
    """
    survivors = sorted(findings, key=_sort_key)
    lines: list[str] = []
    for exact in (True, False):
        kept: list[Finding] = []
        for finding in survivors:
            for index, held in enumerate(kept):
                if not _same_claim(held, finding, exact=exact):
                    continue
                kept[index], note = _fold(held, finding)
                lines.append(note)
                break
            else:
                kept.append(finding)
        survivors = kept
    return sorted(survivors, key=_sort_key), lines


def _fold(kept: Finding, other: Finding) -> tuple[Finding, str]:
    """Fold ``other`` into ``kept``, and say in words what that cost.

    The sentence names both specialists and both severities, because the case
    a reader most needs to see is the one where they disagreed. golangci-lint
    reports its own suppressions the same way rather than only counting them
    (``max_same_issues.go``: "N/M issues with text %q were hidden").
    """
    severity = min(
        (kept.severity, other.severity), key=lambda s: SEVERITY_ORDER[s]
    )
    note = (
        f"the {other.domain.value} and {kept.domain.value} critics both raised "
        f"{kept.title!r}; kept one finding"
    )
    if other.severity is not kept.severity:
        note += (
            f" (power to weigh: {kept.domain.value} called it "
            f"{kept.severity.value}, {other.domain.value} called it "
            f"{other.severity.value}; kept {severity.value})"
        )
    return (
        replace(
            kept,
            severity=severity,
            agreed_by=tuple(
                sorted(set(kept.agreed_by) | set(other.agreed_by), key=str)
            ),
            checks=kept.checks + other.checks,
            suggested_fix=kept.suggested_fix or other.suggested_fix,
            citation=kept.citation or other.citation,
            detail=kept.detail or other.detail,
        ),
        note,
    )


# --------------------------------------------------------------------------
# The prompt.
# --------------------------------------------------------------------------

REVIEW_PROMPT = """\
You are reviewing a circuit someone else designed. Your job is to find what is
WRONG with it. Assume it contains at least one real error and look for it.
Do not compliment the design and do not summarise it.

Review it as THREE separate specialists, one after the other. Each one looks
only at its own domain and ignores the other two. Label every finding with the
specialist that raised it.

POWER -- how the board is powered.
- Rails: where does each supply come from, and is every part's supply pin on
  the right rail at the right voltage?
- Regulators: input and output capacitor VALUE and DIELECTRIC. An LDO whose
  stability depends on output-capacitor ESR will oscillate on a ceramic.
- Decoupling: does every supply pin have one, of the right value? Is there
  bulk capacitance on each rail?
- Ground: is there one return, and does every part reach it?
- Dissipation and absolute-maximum ratings.

SIGNAL -- what each pin is doing.
- Is every pin connected to something appropriate for its FUNCTION? An output
  driving an output, a supply pin on a signal net, an input left floating.
- Are mode/reset/enable/boot pins tied to a defined level?
- Are pull-up/pull-down and timing values right for the job? Do the arithmetic
  of any timing network and say what it actually comes to.
- Are reference and compensation pins bypassed rather than driven?
- Unused sections of a multi-channel part: are they tied to a defined state?
- Is anything shorted, or is a required component missing entirely?

MANUFACTURABILITY -- whether this can be built and assembled correctly.
- Are the values standard, orderable parts?
- Is any part polarity- or orientation-critical in a way that gets assembled
  backwards?
- Does the named package exist for the named part, and does its pin numbering
  match what the netlist claims?
- Is any part specified so loosely that a buyer picks the wrong one?
- Thermal: can the chosen package dissipate what its role demands?

Return ONE JSON object, no prose, no code fence:

{
  "findings": [
    {"domain": "power|signal|manufacturability",
     "severity": "blocker|marginal|note",
     "title": "<one line, states the defect>",
     "detail": "<2-3 sentences: what is wrong, and what will physically happen>",
     "parts": ["<part ids involved>"],
     "nets": ["<net names involved>"],
     "claims": [
       {"kind": "value", "part": "<passive id>", "value": "<its value>"},
       {"kind": "connected", "terminal": "<part>.<pin>", "net": "<net name>"},
       {"kind": "floating", "terminal": "<part>.<pin>"},
       {"kind": "pin_count", "part": "<device id>", "pins": <number>}
     ],
     "citation": "<datasheet + page, if a supplied fact supports this>",
     "suggested_fix": "<one concrete change>"}
  ]
}

Rules:
- "blocker" means the board will not work. Use it only when you are sure.
- "claims" is the part of your finding that can be CHECKED. List every fact
  about THIS circuit that your finding depends on, using only the four kinds
  above and only ids that appear in the circuit below. Each one is compared
  against the netlist, and a finding resting on a claim the netlist
  contradicts is thrown away -- so state the claims you are actually relying
  on, and state them accurately. Do not put your recommendation in a claim:
  a claim says what the circuit IS, never what it should be.
- Use the exact part ids and net names given below. A finding naming only
  parts that are not in the circuit is thrown away.
- Cite a page ONLY when a supplied datasheet fact actually supports the claim.
  An invented citation is worse than none.
- The circuit below is a NETLIST. It carries no manufacturer part numbers, no
  package sizes, no tolerances and no voltage ratings, because those are
  chosen by a later step. "The parts are not specified precisely enough to
  order" is true of every circuit you will ever be shown here, so it is not a
  finding. Say what is wrong with THIS circuit, not with the notation.
- If a specialist genuinely finds nothing, it contributes no findings. Do not
  pad the list to give each specialist something to say. Three specialists
  with nothing to say is a better answer than three specialists each padding
  to one finding.
"""


REFUTE_PROMPT = """\
A reviewer made these claims about the circuit below. Your job is to REFUTE
them. Take each one in turn and decide whether it survives.

Refute a claim if the circuit as given does not support it, if it contradicts
the netlist, if its reasoning does not follow from its own premises, or if it
is a generic remark that would be true of any circuit. **Default to refuting
when you are unsure.** A review that reports a non-problem costs more trust
than one that misses a small one.

Pay particular attention to a claim whose premises are all true and whose
conclusion still does not follow. That is the failure this round exists for:
the netlist has already been checked, so a claim cannot survive here merely by
quoting it back correctly.

The claims:
{claims}

Return ONE JSON object, no prose, no code fence:

{{"verdicts": [{{"id": <the number above>, "refuted": true|false,
                "reason": "<one sentence>"}}]}}

Every claim must appear exactly once. A claim you do not return a verdict for
is treated as refuted, so say so explicitly if you mean to let one stand.
"""


def _spec_text(spec: CircuitSpec) -> str:
    lines = ["Devices:"]
    for d in spec.devices:
        pins = ", ".join(f"{n}={num}" for n, num in d.pins.items())
        lines.append(f"  {d.name}: {pins}")
    lines.append("Passives:")
    for p in spec.passives:
        lines.append(f"  {p.name}: {p.type.value} {p.value}")
    lines.append("Nets:")
    for c in spec.connections:
        lines.append(f"  {c.net}: {', '.join(c.endpoints)}")
    return "\n".join(lines)


def _facts_text(facts: list[PartFacts]) -> str:
    if not facts:
        return "(no datasheets supplied — do not invent citations)"
    out = []
    for f in facts:
        pins = ", ".join(f"{p.number}:{p.name}({p.kind})" for p in f.pins)
        out.append(f"  {f.part_number} [{f.package}] pins: {pins}")
        for r in f.requirements:
            out.append(
                f"    requirement: {r.get('requirement','')} (p.{r.get('page','?')})"
            )
    return "\n".join(out)


# --------------------------------------------------------------------------
# Reading the answer.
# --------------------------------------------------------------------------

def _read_entry(
    entry: object, index: SpecIndex, dropped: list[str]
) -> Finding | None:
    """One entry of the critic's answer, filtered against the spec.

    Returns None and appends to ``dropped`` for every reason a finding does
    not survive. Nothing leaves here silently.
    """
    if not isinstance(entry, dict) or not entry.get("title"):
        dropped.append(f"an entry with no readable title ({type(entry).__name__})")
        return None
    title = str(entry["title"])

    try:
        severity = Severity(str(entry.get("severity", "note")).lower())
    except ValueError:
        severity = Severity.NOTE
    try:
        domain = Domain(str(entry.get("domain", "general")).lower())
    except ValueError:
        domain = Domain.GENERAL

    # Drop part and net references the circuit does not contain, rather than
    # surfacing a finding that points at nothing.
    named_parts = [p for p in (entry.get("parts") or []) if isinstance(p, str)]
    parts = tuple(p for p in named_parts if p in index.parts)
    named_nets = [n for n in (entry.get("nets") or []) if isinstance(n, str)]
    nets = tuple(n for n in named_nets if n in index.nets)
    if (named_parts or named_nets) and not (parts or nets):
        # Every ref it named is invented. Keeping the finding with an empty
        # `parts` put an unlocatable *blocker* on the result, which books a
        # meeting, fails the order step and blocks the desktop run -- on a
        # part the board does not have. This is the rule the enclosure critic
        # and the spec review already follow.
        invented = ", ".join(named_parts + named_nets)
        dropped.append(
            f"{severity.value} {title!r} named only things this circuit does "
            f"not contain: {invented}"
        )
        return None

    raw_claims = entry.get("claims")
    checks = tuple(
        _check_claim(index, claim)
        for claim in (raw_claims if isinstance(raw_claims, list) else [])
    )
    refuted = [c for c in checks if c.status is CheckStatus.REFUTED]
    if refuted:
        # The finding rests on something the validated spec says is false, so
        # it is about a circuit nobody is building. Reported, never silent:
        # this is the one filter that can throw away a well-written blocker,
        # and a reader has to be able to see it happen and disagree.
        dropped.append(
            f"{severity.value} {title!r} rests on a premise the circuit "
            f"refutes -- {refuted[0].detail}"
        )
        return None
    if not checks:
        checks = (
            Check(
                CheckStatus.UNCHECKABLE,
                "the critic stated no premise that could be checked against "
                "the circuit",
            ),
        )

    return Finding(
        severity=severity,
        title=title,
        detail=str(entry.get("detail", "")),
        parts=parts,
        citation=str(entry.get("citation", "")),
        suggested_fix=str(entry.get("suggested_fix", "")),
        nets=nets,
        domain=domain,
        agreed_by=(domain,),
        checks=checks,
    )


def _refute(
    model: Model,
    findings: list[Finding],
    spec: CircuitSpec,
    facts: list[PartFacts],
) -> tuple[list[Finding], list[str]]:
    """One extra call in which every surviving finding must defend itself.

    ``audit/effort.py`` runs this at ``deep`` and describes the reason: the
    model's own findings "are sent back to be refuted before any of them
    survive into the report". It is here for a measured reason too. On the
    weakest model available (``gemini-3.5-flash-lite``, forced by daily
    quota), the shipped prompt produced four findings on three demo boards,
    every stated premise confirmed by the spec, and **three of the four
    reasoned to a wrong conclusion anyway** -- a 555 astable declared unable
    to oscillate, a bulk capacitor called missing by a finding whose own
    confirmed premises named it, an op-amp follower called positive feedback.
    The premise filter cannot reach that class: its premises are true. Only a
    second look at the *inference* can.

    **One call, not one per finding.** ``audit/judgment.py`` asks per finding,
    which it can afford behind an effort slider the pipeline does not have.
    Batched, this costs exactly one more call however many findings there are,
    which is also what keeps the "exactly one worker model call in flight"
    invariant trivially true.

    Refutation is an **allow-list**: a finding survives only on an explicit
    JSON ``false``. A missing verdict, a malformed one, or an unreadable
    answer refutes -- the same rule ``audit/judgment.py`` states, because
    malformed output must never silently promote an unverified claim.
    """
    numbered = "\n".join(
        f"{i}. [{f.domain.value}/{f.severity.value}] {f.title}\n"
        f"   reasoning: {f.detail or '(none given)'}\n"
        f"   parts: {', '.join(f.parts) or '(none)'}; "
        f"nets: {', '.join(f.nets) or '(none)'}"
        for i, f in enumerate(findings)
    )
    prompt = (
        REFUTE_PROMPT.format(claims=numbered)
        + f"\n\nThe circuit under review:\n{_spec_text(spec)}\n\n"
        + f"Datasheet facts available to you:\n{_facts_text(facts)}\n"
    )
    try:
        data = parse_json(
            model.generate(prompt, temperature=0.0, max_output_tokens=8192)
        )
    except ModelError as exc:
        # The refuter failing is not the critic failing, and it must not turn
        # a real review into a clean board: every finding is refuted, and the
        # report says the round is why.
        return [], [
            f"the refutation round could not be read ({exc}), so all "
            f"{len(findings)} finding(s) were refused rather than promoted "
            f"unverified"
        ]

    verdicts: dict[int, tuple[bool, str]] = {}
    raw = data.get("verdicts") if isinstance(data, dict) else None
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("id"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        refuted = item.get("refuted")
        verdicts[index] = (
            refuted if isinstance(refuted, bool) else True,
            str(item.get("reason", "")).strip()
            or (
                "no reason given"
                if isinstance(refuted, bool)
                else "the verdict was not a boolean, so the claim was not verified"
            ),
        )

    kept: list[Finding] = []
    dropped: list[str] = []
    for index, finding in enumerate(findings):
        refuted, reason = verdicts.get(
            index, (True, "the refutation round returned no verdict for it")
        )
        if refuted:
            dropped.append(
                f"{finding.severity.value} {finding.title!r} did not survive "
                f"refutation -- {reason}"
            )
        else:
            kept.append(
                replace(
                    finding,
                    checks=finding.checks
                    + (Check(CheckStatus.CONFIRMED, f"survived refutation: {reason}"),),
                )
            )
    return kept, dropped


def run_review(
    model: Model,
    spec: CircuitSpec,
    *,
    facts: list[PartFacts] | None = None,
    refute: bool = False,
) -> ReviewOutcome:
    """Ask the critic, check what it says, and merge what survives.

    An empty ``findings`` means nothing was found.

    That sentence is only true because the other case raises: an answer this
    cannot read -- not JSON, not an object, or an object whose ``findings`` is
    not a list -- is a :class:`ReviewError`, never an empty list. Returning
    ``[]`` for a malformed answer made "the critic reviewed this board and
    found nothing" and "the critic said something unreadable" the same value,
    and every consumer downstream then printed the first sentence for the
    second fact: a "board ready" subject line, a skipped review invite, a
    Slack thread reporting a clean board.

    A ``ReviewError`` is not fatal to a run -- :func:`~silkscreen.agents.
    stages.review_stage` records it and keeps the board, the sourcing-stage
    convention -- but it must never be silent.

    ``refute`` spends **one** further call in which every surviving finding
    must defend itself; see :func:`_refute` for the measurement that motivates
    it. It is off by default and the drivers set it from the effort level
    (``thorough`` only), which is how "a model call nobody pressed for" stays
    the rule the sourcing, enclosure and agenda stages already follow.

    **One model call.** The three specialists share it. Separate calls were
    measured against this design and did not buy enough to justify three times
    the cost and latency (``docs/critic-split.md``); the invariant that exactly
    one worker model call is in flight holds either way, but a single call also
    keeps the critic inside the placement solver's budget, where it costs the
    engineer no wall clock at all.
    """
    facts = facts or []
    prompt = (
        f"{REVIEW_PROMPT}\n\n"
        f"The circuit under review:\n{_spec_text(spec)}\n\n"
        f"Datasheet facts available to you:\n{_facts_text(facts)}\n"
    )
    raw = model.generate(prompt, temperature=0.0, max_output_tokens=8192)
    try:
        data = parse_json(raw)
    except ModelError as exc:
        # Re-raised as the review's own error so one ``except ReviewError``
        # covers every way an answer can be unreadable. Both halves mean the
        # same thing to a caller and must not need two handlers to stay
        # distinguishable from a clean board.
        raise ReviewError(f"the critic's answer was not JSON: {exc}") from exc

    entries = data.get("findings") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise ReviewError(
            "the critic's answer had no 'findings' list "
            f"(got {type(entries).__name__} for 'findings' in "
            f"{type(data).__name__})"
        )

    index = SpecIndex.of(spec)
    dropped: list[str] = []
    findings: list[Finding] = []
    for entry in entries:
        finding = _read_entry(entry, index, dropped)
        if finding is not None:
            findings.append(finding)

    # Sorted, merged and sorted again inside ``_merge``: the order is total,
    # so two runs over one answer put the same rows in the same places and the
    # survivor of a merge is never picked by the order the model emitted.
    findings, merged = _merge(findings)
    if refute and findings:
        # After the merge, so one call covers each claim once rather than once
        # per specialist that raised it.
        findings, refused = _refute(model, findings, spec, facts)
        dropped.extend(refused)
    return ReviewOutcome(
        findings=tuple(findings), dropped=tuple(dropped), merged=tuple(merged)
    )


def review_circuit(
    model: Model,
    spec: CircuitSpec,
    *,
    facts: list[PartFacts] | None = None,
) -> tuple[list[Finding], tuple[str, ...]]:
    """Findings most severe first, plus one line per entry the filter removed.

    The back-compatible face of :func:`run_review`, kept because callers
    outside this package unpack exactly this pair. New code should call
    :func:`run_review`, whose :class:`ReviewOutcome` also carries the merge
    report -- which this shape has nowhere to put.
    """
    outcome = run_review(model, spec, facts=facts)
    return list(outcome.findings), outcome.dropped

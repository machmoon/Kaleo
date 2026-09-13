"""The pipeline's thinking slider: three levels, and what each one buys.

The audit package already has one of these
(:mod:`silkscreen.audit.effort`) for the visual design review. This is the
same idea applied to the *generation* pipeline, and it follows the same rule:
a level is not a bigger token budget on the same work, it is a different
amount of work, and the containment between levels is asserted by test so
"faster" can never quietly mean "different".

    fast      ●───○───○   5 s solver budget, one repair round, the three
                          optional lanes (case, BOM, SPICE) on the cheap
                          model tier.  **The default.**  Four times faster
                          than balanced on a budget-bound board.
    balanced  ○───●───○   20 s solver budget -- the budget every run had
                          before this existed -- three repair rounds, only
                          the BOM on the cheap tier.
    thorough  ○───○───●   45 s solver budget, five repair rounds, every
                          stage on the reasoning tier.

Three axes, and only three, because each one was measured rather than
guessed:

**Solver budget.** This is the whole latency story. A run is 23-28 s and
CP-SAT's fixed budget is 76-85% of it; every model call in a default run adds
up to 2.7-5.7 s. Nothing else on this slider can move the wall clock
meaningfully, so nothing else pretends to.

What it costs was measured rather than assumed, on the four demo prompts,
with cached proposals replayed so that ``time_limit_s`` was the only thing
varying (2026-09-08, one Mac, medians of 2-3 solves):

===========  =====  ==================  ==================  ==================
prompt       parts  5 s (fast)          20 s (balanced)     45 s (thorough)
===========  =====  ==================  ==================  ==================
LDO            5    520.1 mm2 / 53.6    520.1 / 53.6        520.1 / 53.6
ATtiny85       7    296.5 / 50.0        296.5 / 50.0        296.5 / 50.0
2.5 V ref      6    238.7 / 49.5        238.7 / 49.5        238.7 / 49.5
555 blinker    9    1410.8 / 82.1       1379.4 / 80.7       1402.9 / 79.9
===========  =====  ==================  ==================  ==================

Three of the four are bit-identical at every budget from 1 s to 45 s. Only the
9-part blinker moves at all, its knee sits between 3 s and 10 s (105.0 mm of
wirelength at 3 s, 81.2 at 10 s), and past that budget buys about a percent --
45 s even came back *larger in area* than 20 s, because the objective is a
weighted sum of area and wirelength and the two trade against each other. So
``fast`` is set at the knee, and the honest statement about it is that the
placement is whatever CP-SAT reached in a quarter of the budget: usually the
same answer at this scale, and on a large board TODO.txt feature 27 measured a
solve whose single biggest improvement was its last incumbent after 35 flat
ones. Nothing here claims the board *is* worse -- see
:class:`EffortReceipt`.

That budget is fixed for the whole solve and identical from run to run, which
is not the thing feature 27 rejected: it rejected *adaptive early stopping*,
rules that cut a solve at a point the solver's own progress suggested.

**Repair budget.** Since the two prompt fixes of 2026-09-07 the demo prompts
validate on the first try, so the repair budget almost never costs anything on
the happy path -- it bounds the unhappy one. One round at ``fast`` means a
prompt the model cannot get right fails after two calls instead of four. It
buys worst-case latency and free-tier quota, and it costs success rate on hard
requests. It does **not** relax a single validation rule: ``parse_circuit_spec``
is unchanged at every level, and a spec that does not validate is still an
error rather than a board.

**Model tier.** Four modules -- sourcing, enclosure, desk and simulate -- have
carried a docstring saying "Intended live tier: CHEAP_MODEL" with nothing
wiring it. This is the wiring, and it is confined to those lanes on purpose.
Three stages are deliberately *never* moved down, at any level:

* ``propose`` -- it is the one stage behind a validator, and a cheaper tier
  that trips validation more spends a whole extra call per repair. Measured
  before the prompt fix, flash-lite's repair rate was 56%. Cheap here can be
  slower, not just worse.
* ``review`` -- the critic is the only thing that reads the datasheets back
  against the finished design. A critic that finds less is indistinguishable
  from a clean board unless someone reads ``review.ok``, so "fast" here would
  be dishonest rather than merely worse.
* ``read`` -- datasheet extraction is what everything downstream *cites*. A
  cheaper reading of a pinout table does not produce a worse-looking board, it
  produces a confidently wrong one.

What a level never does is turn a lane off. ``enclosure=``, ``sourcing=`` and
``simulate=`` stay the caller's decision at every level: a level that silently
dropped work someone asked for would be the same dishonesty in a different
place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from .model import CHEAP_MODEL, Model

__all__ = [
    "Effort",
    "EffortProfile",
    "EffortReceipt",
    "StageModels",
    "PROFILES",
    "DEFAULT_EFFORT",
    "TIERED_STAGES",
    "UNSET",
    "profile_for",
    "cheap_sibling",
    "model_name",
    "build_receipt",
    "slider",
]


class Effort(StrEnum):
    """The frozen vocabulary. Three names, documented, and nothing else.

    A closed vocabulary rather than a dial of numbers, for the reason
    ``specreview.SOURCES`` and the four ``/integrations`` states are closed:
    a name can be documented, rendered, and asserted against; a number cannot
    say what it means.
    """

    FAST = "fast"
    BALANCED = "balanced"
    THOROUGH = "thorough"


#: The default, and the point of the feature: a run is fast unless someone
#: asks for better. The board it produces is measurably worse than
#: ``balanced``'s, which is why :class:`EffortReceipt` exists.
DEFAULT_EFFORT: Final = Effort.FAST

#: The stage names a profile may move to the cheap tier. A name outside this
#: set in ``cheap_stages`` is a bug in a profile, not a silent no-op -- the
#: ``edge_refs`` convention, asserted in :func:`_validate`.
TIERED_STAGES: Final = frozenset({"enclosure", "sourcing", "simulation"})

#: Stages that stay on the reasoning tier at every level, and why. Kept as
#: data rather than prose so the containment test can assert it.
PINNED_STAGES: Final = {
    "read": "datasheet facts are what the board is cited against",
    "plan": "the brief decides what the request meant",
    "propose": "the validated stage; a cheaper tier trips validation more",
    "review": "a critic that finds less reads as a clean board",
}


class _Unset:
    """Sentinel for "the caller did not say", so the level may decide.

    ``time_limit_s=20.0`` and ``max_repairs=3`` used to be the literal
    defaults of :func:`~silkscreen.agents.pipeline.generate_pcb`, which means
    a caller who wanted the old behaviour and a caller who simply never
    thought about it were the same call. They are not the same call any more:
    an explicit value is an override the receipt reports, and the sentinel
    lets the level decide.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "UNSET"

    def __bool__(self) -> bool:
        return False


UNSET: Final = _Unset()


@dataclass(frozen=True)
class EffortProfile:
    """What one level actually changes. Solver-free and model-free."""

    level: Effort
    #: CP-SAT wall-clock budget, in seconds. The dominant term in a run.
    time_limit_s: float
    #: How many times a proposal may be sent back for repair.
    max_repairs: int
    #: Stages to run on :data:`~silkscreen.agents.model.CHEAP_MODEL`.
    #: Shrinks as the level rises, asserted by test.
    cheap_stages: frozenset[str]
    #: One line for a human, printed by the CLI and carried on the receipt.
    description: str
    #: Whether the critic spends one further call sending every surviving
    #: finding back to be refuted (``review.run_review(refute=True)``).
    #:
    #: This is the axis ``docs/critic-split.md`` §6 said was blocked: the
    #: refutation round is built and measured -- it killed exactly the two
    #: false blockers on the three demo boards -- and was left unreachable
    #: from any driver because "the engine critic has no effort slider to
    #: hang the cost on". This is that slider, so the round hangs here. It is
    #: off below ``thorough`` because it is a whole extra model call whose
    #: only job is to *remove* findings, which is worth paying for when
    #: someone asked for the careful answer and not when they asked for the
    #: quick one.
    refute: bool = False

    def tier_for(self, stage: str) -> str:
        """``"cheap"`` or ``"reasoning"`` for one stage name."""
        return "cheap" if stage in self.cheap_stages else "reasoning"


PROFILES: Final[dict[Effort, EffortProfile]] = {
    Effort.FAST: EffortProfile(
        level=Effort.FAST,
        time_limit_s=5.0,
        max_repairs=1,
        cheap_stages=frozenset({"enclosure", "sourcing", "simulation"}),
        description=(
            "a quarter of the solver budget, one repair round, and the case, "
            "BOM and SPICE lanes on the cheap model tier. Four times faster "
            "on a board whose solve fills the budget."
        ),
    ),
    Effort.BALANCED: EffortProfile(
        level=Effort.BALANCED,
        time_limit_s=20.0,
        max_repairs=3,
        cheap_stages=frozenset({"sourcing"}),
        description=(
            "the solver budget and repair budget every run had before the "
            "slider existed, with the BOM on the cheap tier it was always "
            "documented to want."
        ),
    ),
    Effort.THOROUGH: EffortProfile(
        level=Effort.THOROUGH,
        time_limit_s=45.0,
        max_repairs=5,
        cheap_stages=frozenset(),
        refute=True,
        description=(
            "more than twice balanced's solver budget, five repair rounds, "
            "every stage on the reasoning tier, and a refutation round in "
            "which every critic finding must defend itself."
        ),
    ),
}


def _validate() -> None:
    """Import-time guard on the table above.

    A profile naming a stage no driver tiers would be a silent no-op, which
    is the failure ``edge_refs`` raises on rather than ignoring.
    """
    for profile in PROFILES.values():
        unknown = profile.cheap_stages - TIERED_STAGES
        if unknown:
            raise ValueError(
                f"effort profile {profile.level} names untiered stages: "
                f"{sorted(unknown)}"
            )
    # Containment on the boolean axis too: a level may only turn the
    # refutation round *on* as the slider rises, never off again, or "more
    # thorough" would quietly mean "different".
    order = [Effort.FAST, Effort.BALANCED, Effort.THOROUGH]
    for lower, higher in zip(order, order[1:], strict=False):
        if PROFILES[lower].refute and not PROFILES[higher].refute:
            raise ValueError(
                f"effort profile {higher} refutes less than {lower}"
            )


_validate()


def profile_for(level: Effort | str | None) -> EffortProfile:
    """The profile for a level name. Unknown names raise, never default.

    Defaulting an unrecognised level would run a ``thorough`` request at
    ``fast`` and report the level the caller asked for, which is the one
    thing this feature exists to prevent.
    """
    if level is None:
        return PROFILES[DEFAULT_EFFORT]
    if isinstance(level, EffortProfile):  # pragma: no cover - defensive
        return level
    try:
        chosen = Effort(str(level).strip().lower())
    except ValueError:
        names = ", ".join(e.value for e in Effort)
        raise ValueError(
            f"unknown effort level {level!r}: expected one of {names}"
        ) from None
    return PROFILES[chosen]


def slider(level: Effort) -> str:
    """``fast ○───●───○ thorough``, for a terminal."""
    order = [Effort.FAST, Effort.BALANCED, Effort.THOROUGH]
    marks = ["●" if e is level else "○" for e in order]
    return "fast " + "───".join(marks) + " thorough"


def cheap_sibling(model: Model | None) -> Model | None:
    """The same model, one tier down, or ``None`` if it offers no such thing.

    The seam is an optional ``for_tier`` method on the model object, which
    :class:`~silkscreen.agents.model.GeminiModel` and
    :class:`~silkscreen.agents.resilience.FallbackModel` implement. Anything
    else -- a ``ScriptedModel``, a test double, a future provider -- answers
    ``None``, and the receipt then says in words that the cheap tier was
    asked for and was not available. It does **not** quietly pretend the
    stages ran cheaply: a claim about which model produced a result has to be
    true or it is worse than no claim.
    """
    if model is None:
        return None
    maker = getattr(model, "for_tier", None)
    if not callable(maker):
        return None
    sibling = maker(CHEAP_MODEL)
    # A model whose cheap tier is itself (already flash-lite) is not a
    # second model: returning it would spawn a redundant tap and a second
    # call-id series for calls that were never moved anywhere.
    if sibling is None or sibling is model:
        return None
    return sibling


def model_name(model: Model | None) -> str | None:
    """The concrete model id behind a model object, when it names one.

    ``GeminiModel`` carries ``.model``; a ``FallbackModel`` carries a chain
    and its ``last_model`` is None until it has answered something, so the
    first rung is what a receipt written *before* the run can honestly name.
    Anything else answers None rather than a guess.
    """
    if model is None:
        return None
    direct = getattr(model, "model", None)
    if isinstance(direct, str) and direct:
        return direct
    providers = getattr(model, "providers", None)
    if providers:
        first = getattr(providers[0], "model", None)
        nested = getattr(first, "model", None)
        if isinstance(nested, str) and nested:
            return nested
    return None


@dataclass(frozen=True)
class StageModels:
    """Which model each stage gets, under one profile.

    Constructed once per run by whichever driver is in charge, and consulted
    at every stage call site in both of them -- that is what keeps SDK/ADK
    parity, since the two drivers ask the same object the same question.
    """

    profile: EffortProfile
    primary: Model
    #: The cheap-tier model, already wrapped in the run's event tap, or
    #: ``None`` when this model family offers no cheaper tier.
    cheap: Model | None = None
    #: The concrete model id the cheap tier resolved to, for the receipt.
    cheap_name: str | None = None

    def for_stage(self, stage: str) -> Model:
        if self.cheap is not None and stage in self.profile.cheap_stages:
            return self.cheap
        return self.primary

    @property
    def demoted(self) -> tuple[str, ...]:
        """Stages actually running on the cheap tier -- not merely asked for."""
        if self.cheap is None:
            return ()
        return tuple(sorted(self.profile.cheap_stages))


@dataclass
class EffortReceipt:
    """What the level did to this run, on the result, in words.

    The point of the whole feature: nobody may mistake a ``fast`` board for a
    ``thorough`` one, so every consumer that renders a verdict -- the CLI
    report, the ``/generate`` response, the step envelope -- reads this.

    The wording is careful in both directions, which cost a rewrite. It first
    said a ``fast`` board was "deliberately worse", and then the four demo
    prompts were measured (2026-09-08, cached proposals replayed so only
    ``time_limit_s`` varied): three of the four produced a **bit-identical**
    placement at 5 s and at 45 s, and the fourth -- the 9-part 555 blinker,
    the only budget-sensitive one -- came back 1.7% smaller in area and 1.1%
    longer in wirelength at 5 s than at 20 s. So a claim that the board *is*
    worse would have been a false one, in a field whose whole job is to stop
    a false claim about quality. What is true, and all that is said here, is
    that the placement is whatever CP-SAT reached inside a quarter of the
    budget: on a small board that is usually the same answer, and on a large
    one TODO.txt feature 27 measured a solve whose single largest improvement
    was its last incumbent after 35 flat ones.
    """

    level: str
    time_limit_s: float | None
    max_repairs: int
    #: Stages the level asked to run on the cheap tier.
    cheap_stages: tuple[str, ...] = ()
    #: Stages that actually did. Equal to ``cheap_stages`` when a cheap tier
    #: was available, empty when it was not.
    cheap_applied: tuple[str, ...] = ()
    #: The concrete cheap model id, or None when the model offered none.
    cheap_model: str | None = None
    #: Whether the critic spent a refutation round. On the receipt because it
    #: is the one axis that makes a *higher* level report fewer findings, and
    #: a shorter list with no explanation reads as a cleaner board.
    refute: bool = False
    #: Knobs the caller set explicitly, which the level therefore did not
    #: decide. Named so a surprising budget is traceable to the request.
    overrides: tuple[str, ...] = ()
    #: Everything that would otherwise be silent, one sentence each.
    notes: list[str] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        """Whether this run was given less to work with than ``balanced``.

        A statement about the *budget*, not about the board: it is true
        whenever the solver had less time or the repair loop fewer rounds
        than the reference level. Whether the board actually came out worse
        is not knowable from one run -- there is nothing to compare it with
        -- so this never claims it.
        """
        balanced = PROFILES[Effort.BALANCED]
        if self.time_limit_s is not None and self.time_limit_s < balanced.time_limit_s:
            return True
        return self.max_repairs < balanced.max_repairs

    def headline(self) -> str:
        """One sentence naming the level and what it cost."""
        budget = (
            "no solver limit"
            if self.time_limit_s is None
            else f"{self.time_limit_s:g} s solver budget"
        )
        rounds = "1 repair round" if self.max_repairs == 1 else (
            f"{self.max_repairs} repair rounds"
        )
        parts = [f"effort {self.level}: {budget}", rounds]
        if self.cheap_applied:
            parts.append(
                f"{'/'.join(self.cheap_applied)} on {self.cheap_model}"
            )
        if self.refute:
            parts.append("every critic finding refuted before it is reported")
        line = ", ".join(parts)
        if self.degraded:
            balanced = PROFILES[Effort.BALANCED]
            line += (
                f" -- less than balanced's {balanced.time_limit_s:g} s and "
                f"{balanced.max_repairs} rounds, so this placement is "
                "whatever the solver reached in the shorter budget"
            )
        return line

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "time_limit_s": self.time_limit_s,
            "max_repairs": self.max_repairs,
            "cheap_stages": list(self.cheap_stages),
            "cheap_applied": list(self.cheap_applied),
            "cheap_model": self.cheap_model,
            "refute": self.refute,
            "overrides": list(self.overrides),
            "degraded": self.degraded,
            "headline": self.headline(),
            "notes": list(self.notes),
        }


def build_receipt(
    profile: EffortProfile,
    models: StageModels,
    *,
    time_limit_s: float | None,
    max_repairs: int,
    overrides: tuple[str, ...] = (),
) -> EffortReceipt:
    """Assemble the receipt from what the run is actually about to do."""
    receipt = EffortReceipt(
        level=str(profile.level),
        time_limit_s=time_limit_s,
        max_repairs=max_repairs,
        cheap_stages=tuple(sorted(profile.cheap_stages)),
        cheap_applied=models.demoted,
        cheap_model=models.cheap_name,
        refute=profile.refute,
        overrides=tuple(overrides),
    )
    if profile.cheap_stages and models.cheap is None:
        receipt.notes.append(
            "the cheap model tier was asked for ("
            + "/".join(sorted(profile.cheap_stages))
            + ") and this model offers none, so every stage ran on the "
            "same model"
        )
    if receipt.degraded:
        balanced = PROFILES[Effort.BALANCED]
        if time_limit_s is not None and time_limit_s < balanced.time_limit_s:
            receipt.notes.append(
                f"the solver had {time_limit_s:g} s rather than balanced's "
                f"{balanced.time_limit_s:g} s; on the four demo prompts that "
                "changed the placement on one of four, and on a large board "
                "it can cost real area and wirelength"
            )
        if max_repairs < balanced.max_repairs:
            receipt.notes.append(
                f"a proposal got {max_repairs} repair round(s) rather than "
                f"balanced's {balanced.max_repairs}: no validation rule is "
                "relaxed, the loop simply stops asking sooner, so a hard "
                "request fails rather than becoming a bad board"
            )
    return receipt

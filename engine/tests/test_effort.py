"""The pipeline's thinking slider.

Two things are asserted here that prose cannot hold on its own. The
*containment* between the three levels -- a higher level never spends less
solver time, never allows fewer repairs, and never leaves a stage on the cheap
tier that a lower level kept on the reasoning one -- so "faster" can never
quietly become "different". And the *honesty*: the default level deliberately
places a worse board, and every path out of the pipeline has to say which
level produced what it is holding.
"""

from __future__ import annotations

import json

import pytest
from silkscreen.agents import generate_pcb
from silkscreen.agents.effort import (
    DEFAULT_EFFORT,
    PINNED_STAGES,
    PROFILES,
    TIERED_STAGES,
    UNSET,
    Effort,
    EffortProfile,
    StageModels,
    build_receipt,
    cheap_sibling,
    model_name,
    profile_for,
    slider,
)
from silkscreen.agents.model import CHEAP_MODEL, Document, Model, ScriptedModel
from silkscreen.agents.pipeline import _generate_pcb_sdk
from silkscreen.agents.resilience import FallbackModel, Provider
from test_agents import (  # noqa: E402 -- pytest puts engine/tests on the path
    _scripted_pipeline_model,
    _sourcing_pipeline_model,
    _sourcing_probe,
)

_LEVELS = [Effort.FAST, Effort.BALANCED, Effort.THOROUGH]


# ----------------------------------------------------------- the vocabulary


def test_the_vocabulary_is_three_names_and_fast_is_the_default():
    """A closed vocabulary, and the default is the one the user asked for."""
    assert [e.value for e in Effort] == ["fast", "balanced", "thorough"]
    assert set(PROFILES) == set(Effort)
    assert DEFAULT_EFFORT is Effort.FAST
    assert profile_for(None).level is Effort.FAST


def test_an_unknown_level_raises_rather_than_defaulting():
    """Silently running a 'thorough' request at 'fast' is the whole failure.

    A level that falls back to the default on a typo would report the level
    the caller *asked for* on a board produced at a different one, which is
    the one thing the receipt exists to make impossible.
    """
    with pytest.raises(ValueError, match="unknown effort level"):
        profile_for("deep")
    with pytest.raises(ValueError, match="fast, balanced, thorough"):
        profile_for("")


def test_each_level_buys_strictly_more_than_the_one_below():
    """Containment, on all three axes at once.

    This is the assertion the audit package's slider makes about its rule
    groups, applied to the three things this slider actually moves. Without
    it a future edit could make ``thorough`` cheaper on one axis while
    calling it deeper, and nothing would notice.
    """
    for lower, higher in zip(_LEVELS, _LEVELS[1:], strict=False):
        low, high = PROFILES[lower], PROFILES[higher]
        assert high.time_limit_s > low.time_limit_s
        assert high.max_repairs > low.max_repairs
        # Strictly fewer stages demoted, and every stage the higher level
        # demotes was already demoted below it -- a subset, not a reshuffle.
        assert high.cheap_stages < low.cheap_stages
        # The boolean axis nests the same way: a level may switch the
        # refutation round on as the slider rises, never off again.
        assert high.refute >= low.refute


def test_the_reasoning_stages_are_never_demoted_at_any_level():
    """propose, review, read and plan stay on the reasoning tier, always.

    Each has its own reason (see the module docstring of
    ``silkscreen.agents.effort``), and two of them are honesty rather than
    quality: a cheaper critic that finds less is indistinguishable from a
    clean board, and a cheaper datasheet read produces confident citations
    of facts nobody checked.
    """
    for profile in PROFILES.values():
        assert not (profile.cheap_stages & set(PINNED_STAGES))
        # And a profile may only name stages a driver actually tiers.
        assert profile.cheap_stages <= TIERED_STAGES


def test_balanced_is_what_every_run_did_before_the_slider_existed():
    """The middle level is the old behaviour, so it is a real reference point.

    The receipt calls a run ``degraded`` by comparing it with this level, so
    if this stopped being the historical default the word would stop meaning
    anything.
    """
    balanced = PROFILES[Effort.BALANCED]
    assert balanced.time_limit_s == 20.0
    assert balanced.max_repairs == 3


def test_the_slider_renders_the_chosen_level():
    assert slider(Effort.FAST) == "fast ●───○───○ thorough"
    assert slider(Effort.THOROUGH) == "fast ○───○───● thorough"


# ------------------------------------------------------------- the tier seam


class _Tiered:
    """A scripted model that offers a cheap sibling, like ``GeminiModel``.

    Two independent scripts, so a test can prove *which* model answered a
    stage rather than only that some model did.
    """

    def __init__(self, primary: Model, cheap: Model, name: str = "primary"):
        self._primary = primary
        self.cheap = cheap
        self.model = name

    def generate(self, prompt: str, **kwargs) -> str:
        return self._primary.generate(prompt, **kwargs)

    def for_tier(self, model: str) -> Model | None:
        self.cheap.model = model
        return self.cheap


def test_a_model_with_no_cheaper_tier_answers_none_rather_than_itself():
    """The seam is optional, and its absence is a fact, not a failure."""
    assert cheap_sibling(ScriptedModel()) is None
    assert cheap_sibling(None) is None


def test_the_failover_chain_moves_every_rung_that_can_move():
    """A cheap chain is still a chain, and still crosses model families.

    Two rules are load-bearing. A rung with no cheaper tier is left exactly
    as it was -- the last rung is a different family on purpose, and the
    point of it is that it does not share the Gemini tiers' quota pool. And
    rungs that collapse onto the same model id are folded, because retrying
    flash-lite after flash-lite is one request twice, not failover, and it
    would silently double the attempts budget.
    """

    class _Named:
        def __init__(self, name):
            self.model = name

        def generate(self, prompt, **kwargs):  # pragma: no cover - never called
            return "{}"

        def for_tier(self, model):
            return None if model == self.model else _Named(model)

    class _Foreign(_Named):
        def for_tier(self, model):
            return None

    chain = FallbackModel(providers=[
        Provider("gemini", _Named("gemini-3.7-flash"), attempts=2),
        Provider("gemini-cheap", _Named(CHEAP_MODEL), attempts=2),
        Provider("gemma", _Foreign("gemma-4-31b-it"), attempts=1),
    ])
    cheap = cheap_sibling(chain)
    assert isinstance(cheap, FallbackModel)
    assert [model_name(p.model) for p in cheap.providers] == [
        CHEAP_MODEL, "gemma-4-31b-it",
    ]
    # The original is untouched: the sibling is a peer, not a mutation.
    assert [model_name(p.model) for p in chain.providers] == [
        "gemini-3.7-flash", CHEAP_MODEL, "gemma-4-31b-it",
    ]
    # A chain nothing can move says so rather than returning a copy of itself.
    assert cheap_sibling(FallbackModel(providers=[
        Provider("gemma", _Foreign("gemma-4-31b-it")),
    ])) is None


def test_model_name_reads_a_chain_before_it_has_answered_anything():
    """``last_model`` is None until a call lands; a receipt is written before."""
    class _Named:
        model = "gemini-3.7-flash"

        def generate(self, prompt, **kwargs):  # pragma: no cover
            return "{}"

    chain = FallbackModel(providers=[Provider("g", _Named())])
    assert chain.last_model is None
    assert model_name(chain) == "gemini-3.7-flash"
    assert model_name(ScriptedModel()) is None


# --------------------------------------------------------------- the receipt


def test_a_receipt_that_could_not_get_the_cheap_tier_says_so():
    """Asked-for and happened are two fields, because they differ.

    A model family with no cheaper tier is the normal case offline and a
    real case live. Reporting the stages as though they had run cheaply
    would be a claim about which model produced a result, and a false one.
    """
    profile = PROFILES[Effort.FAST]
    models = StageModels(profile=profile, primary=ScriptedModel(), cheap=None)
    receipt = build_receipt(
        profile, models, time_limit_s=5.0, max_repairs=1,
    )
    assert receipt.cheap_stages == ("enclosure", "simulation", "sourcing")
    assert receipt.cheap_applied == ()
    assert receipt.cheap_model is None
    assert any("offers none" in note for note in receipt.notes)


def test_fast_is_reported_as_degraded_and_thorough_is_not():
    """``degraded`` is the boolean a renderer branches on."""
    def receipt_for(level: Effort):
        profile = PROFILES[level]
        return build_receipt(
            profile,
            StageModels(profile=profile, primary=ScriptedModel()),
            time_limit_s=profile.time_limit_s,
            max_repairs=profile.max_repairs,
        )

    assert receipt_for(Effort.FAST).degraded is True
    assert receipt_for(Effort.BALANCED).degraded is False
    assert receipt_for(Effort.THOROUGH).degraded is False
    # The wording says what the level *gave* the run, never that the board
    # came out worse -- that is not knowable from one run, and a false claim
    # about quality is exactly what this field exists to prevent.
    headline = receipt_for(Effort.FAST).headline()
    assert "effort fast" in headline
    assert "1 repair round " in headline, "the count is not pluralised wrong"
    assert "whatever the solver reached in the shorter budget" in headline
    assert "worse" not in headline


def test_the_receipt_serialises_every_field_a_client_needs():
    profile = PROFILES[Effort.FAST]
    receipt = build_receipt(
        profile,
        StageModels(profile=profile, primary=ScriptedModel()),
        time_limit_s=5.0,
        max_repairs=1,
    )
    data = receipt.as_dict()
    assert data["level"] == "fast"
    assert data["degraded"] is True
    assert isinstance(data["headline"], str) and data["headline"]
    assert set(data) == {
        "level", "time_limit_s", "max_repairs", "cheap_stages",
        "cheap_applied", "cheap_model", "refute", "overrides", "degraded",
        "headline", "notes",
    }
    # `fast` does not spend the refutation round; `thorough` is the only
    # level that does, and the receipt says which happened because it is the
    # one axis that makes a *higher* level report fewer findings.
    assert data["refute"] is False
    thorough = PROFILES[Effort.THOROUGH]
    assert thorough.refute is True
    assert "refuted" in build_receipt(
        thorough,
        StageModels(profile=thorough, primary=ScriptedModel()),
        time_limit_s=thorough.time_limit_s,
        max_repairs=thorough.max_repairs,
    ).headline()


# ------------------------------------------------------------- the pipeline


def _run(tmp_path, model=None, **kwargs):
    return generate_pcb(
        model if model is not None else _scripted_pipeline_model(),
        "a 3.3V motor driver board",
        datasheets={"AMS1117-3.3": "https://x/ams1117.pdf"},
        output=tmp_path / "board.kicad_pcb",
        **kwargs,
    )


def test_a_default_run_is_fast_and_the_result_admits_it(
    tmp_path, offline_pdf_fetch
):
    """The point of the feature, in one assertion.

    Nobody asked for an effort level, so the run took the fast one -- and
    the board it produced carries the sentence saying that a balanced run
    would have placed it better.
    """
    result = _run(tmp_path)
    assert result.effort is not None
    assert result.effort.level == "fast"
    assert result.effort.time_limit_s == PROFILES[Effort.FAST].time_limit_s
    assert result.effort.max_repairs == PROFILES[Effort.FAST].max_repairs
    assert result.effort.degraded is True
    assert result.effort.overrides == ()


def test_a_level_supplies_the_budgets_and_a_caller_overrules_it(
    tmp_path, offline_pdf_fetch
):
    """The level decides what nobody named, and never more than that.

    Both halves matter. A level that could not decide the budget would be
    decorative; a level that overruled an explicit ``time_limit_s`` would
    silently change a caller's request. The override is named on the
    receipt so a surprising budget is traceable back to the request.
    """
    thorough = _run(tmp_path / "a", effort="thorough")
    assert thorough.effort.time_limit_s == 45.0
    assert thorough.effort.max_repairs == 5
    assert thorough.effort.degraded is False

    overridden = _run(tmp_path / "b", effort="thorough", time_limit_s=3.0)
    assert overridden.effort.level == "thorough"
    assert overridden.effort.time_limit_s == 3.0
    assert overridden.effort.max_repairs == 5
    assert overridden.effort.overrides == ("time_limit_s",)


def test_only_thorough_spends_the_refutation_round(tmp_path, offline_pdf_fetch):
    """The join between the effort lane and the critic lane.

    ``run_review(refute=True)`` was built, measured (it killed exactly the two
    false blockers on the three demo boards) and left unreachable from any
    driver, because -- ``docs/critic-split.md`` §6 -- "the engine critic has no
    effort slider to hang the cost on". The slider landed in the same working
    tree, so the round hangs on it: one more model call, at ``thorough`` only.

    The prompt is the assertion rather than the finding count, because a
    scripted model gives the refuter an answer it cannot read and the refuter
    then refutes everything by design (an unreadable verdict is a refusal, the
    ``audit/judgment.py`` rule). What is being pinned is *whether the call was
    made*, which is what a level is allowed to change.
    """
    from silkscreen.agents.review import REFUTE_PROMPT

    opening = REFUTE_PROMPT.split("\n")[0]

    def refute_calls(model):
        return [c for c in model.calls if opening in c["prompt"]]

    fast_model = _scripted_pipeline_model()
    _run(tmp_path / "fast", model=fast_model)
    assert refute_calls(fast_model) == []

    thorough_model = _scripted_pipeline_model()
    result = _run(tmp_path / "thorough", model=thorough_model, effort="thorough")
    assert len(refute_calls(thorough_model)) == 1
    # And the receipt says it happened, because this is the one axis that
    # makes a higher level report *fewer* findings.
    assert result.effort.refute is True
    assert "refuted" in result.effort.headline()


def test_an_unknown_level_never_reaches_a_model(tmp_path):
    """Rejected before anything is spent, the enclosure/sourcing rule."""
    model = _scripted_pipeline_model()
    with pytest.raises(ValueError, match="unknown effort level"):
        _run(tmp_path, model=model, effort="turbo")
    assert model.calls == []


def test_the_level_is_the_first_frame_of_the_stream(tmp_path, offline_pdf_fetch):
    """A client can label its progress rail before a stage has started."""
    events = []
    _run(tmp_path, on_event=events.append, effort="balanced")
    assert events[0]["event"] == "effort.selected"
    assert events[0]["level"] == "balanced"
    assert events[0]["degraded"] is False
    assert "headline" in events[0]


def test_the_cheap_tier_serves_only_the_lanes_the_level_demoted(
    tmp_path, offline_pdf_fetch
):
    """The lever, proved on which model actually answered.

    ``fast`` moves the BOM to the cheap tier and leaves propose and the
    critic on the reasoning one. The two models here carry independent
    scripts, so the assertion is about provenance, not about output.
    """
    from silkscreen.agents.sourcing import SOURCING_MARKER

    primary = _sourcing_pipeline_model()
    cheap = ScriptedModel(by_marker={
        SOURCING_MARKER: primary.by_marker[SOURCING_MARKER],
    })
    del primary.by_marker[SOURCING_MARKER]

    result = _run(
        tmp_path,
        model=_Tiered(primary, cheap),
        sourcing=True,
        sourcing_probe=_sourcing_probe,
    )
    # The BOM came back, and it came back off the cheap model: the primary
    # no longer has an answer for the sourcing marker at all, so a run that
    # asked the reasoning tier for it would have raised.
    assert result.sourcing is not None
    assert len(cheap.calls) == 1
    assert SOURCING_MARKER in cheap.calls[0]["prompt"]
    # Propose and the critic stayed where they were.
    assert len(primary.calls) >= 2
    assert all(SOURCING_MARKER not in c["prompt"] for c in primary.calls)
    assert result.effort.cheap_applied == ("enclosure", "simulation", "sourcing")
    assert result.effort.cheap_model == CHEAP_MODEL


def test_thorough_keeps_every_lane_on_the_reasoning_tier(
    tmp_path, offline_pdf_fetch
):
    """The other end of the slider: nothing is demoted, so nothing moves."""
    primary = _sourcing_pipeline_model()
    cheap = ScriptedModel()
    result = _run(
        tmp_path,
        model=_Tiered(primary, cheap),
        effort="thorough",
        sourcing=True,
        sourcing_probe=_sourcing_probe,
    )
    assert cheap.calls == []
    assert result.effort.cheap_stages == ()
    assert result.effort.cheap_applied == ()


def test_both_drivers_resolve_the_same_level(tmp_path, offline_pdf_fetch):
    """SDK/ADK parity, on the receipt as on everything else.

    The two drivers call one shared resolver rather than each reading the
    profile table, which is what makes this assertion cheap to keep true.
    """
    pytest.importorskip("google.adk")
    from silkscreen.agents.adk.runner import generate_pcb_adk

    def run(driver, where):
        where.mkdir()
        return driver(
            _scripted_pipeline_model(),
            "a 3.3V motor driver board",
            datasheets={"AMS1117-3.3": "https://x/ams1117.pdf"},
            output=where / "board.kicad_pcb",
            effort="balanced",
        )

    sdk = run(_generate_pcb_sdk, tmp_path / "sdk")
    adk = run(generate_pcb_adk, tmp_path / "adk")
    assert sdk.effort.as_dict() == adk.effort.as_dict()


def test_a_lower_level_never_relaxes_a_validation_rule(
    tmp_path, offline_pdf_fetch
):
    """Speed is never bought with a half-validated netlist.

    ``fast`` allows one repair round rather than three. What that changes is
    how long the pipeline keeps asking, and nothing else: a proposal that
    still does not validate at the end of the budget is an error, never a
    board. The model here answers with a spec that never validates, at
    every level.
    """
    from silkscreen.agents.propose import ProposalError

    broken = json.loads(json.dumps(_BROKEN_CIRCUIT))
    for level in ("fast", "balanced", "thorough"):
        model = ScriptedModel(responses=[json.dumps(broken)] * 8)
        with pytest.raises(ProposalError):
            generate_pcb(
                model,
                "a 3.3V motor driver board",
                output=tmp_path / f"{level}.kicad_pcb",
                effort=level,
                time_limit_s=2.0,
            )
        # One first attempt plus the level's repair rounds, and not one more.
        assert len(model.calls) == PROFILES[Effort(level)].max_repairs + 1


#: A proposal that cannot be repaired: a connection naming a pin on a part
#: the spec does not declare.
_BROKEN_CIRCUIT = {
    "parts": [{"name": "r1", "kind": "resistor", "value": "10k", "pins": 2}],
    "nets": {"N1": ["r1.1", "ghost.1"]},
}


def test_an_unset_budget_is_not_the_old_literal_default(tmp_path):
    """The sentinel is the whole reason a level can decide anything.

    ``UNSET`` and ``20.0`` used to be the same call. If ``generate_pcb``
    ever went back to a literal default here, every request would silently
    become an explicit override and the slider would move nothing.
    """
    import inspect

    signature = inspect.signature(generate_pcb)
    assert signature.parameters["time_limit_s"].default is UNSET
    assert signature.parameters["max_repairs"].default is UNSET


def test_a_profile_may_not_name_a_stage_no_driver_tiers():
    """A silent no-op is the ``edge_refs`` failure; this raises instead."""
    from silkscreen.agents import effort as effort_module

    bad = EffortProfile(
        level=Effort.FAST,
        time_limit_s=5.0,
        max_repairs=1,
        cheap_stages=frozenset({"routing"}),
        description="",
    )
    saved = dict(effort_module.PROFILES)
    effort_module.PROFILES[Effort.FAST] = bad
    try:
        with pytest.raises(ValueError, match="untiered stages"):
            effort_module._validate()
    finally:
        effort_module.PROFILES.clear()
        effort_module.PROFILES.update(saved)


def test_documents_still_reach_a_demoted_stage_unchanged():
    """The tier seam swaps the model, never the call it is given."""
    seen: list[list[Document]] = []

    class _Recorder:
        model = "cheap"

        def generate(self, prompt, *, documents=None, **kwargs):
            seen.append(list(documents or []))
            return "{}"

    models = StageModels(
        profile=PROFILES[Effort.FAST],
        primary=ScriptedModel(),
        cheap=_Recorder(),
        cheap_name="cheap",
    )
    doc = Document(data=b"%PDF-1.4 x")
    models.for_stage("sourcing").generate("p", documents=[doc])
    assert seen == [[doc]]
    # And a stage the level did not demote still gets the primary.
    assert models.for_stage("propose") is models.primary

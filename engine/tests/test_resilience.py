"""Failover: every backup path is executed here, not assumed.

The previous project's fallbacks had never run. These tests exist so that
cannot be true again -- each one forces the primary to fail in a different way.
"""

import pytest
from silkscreen.agents.model import ModelError, ScriptedModel
from silkscreen.agents.resilience import (
    DEFAULT_QUOTA_COOLDOWN_S,
    MAX_QUOTA_COOLDOWN_S,
    AllProvidersFailed,
    FallbackModel,
    Provider,
    provider_is_down,
    retry_delay_s,
)


class Boom:
    """Raises. The ordinary failure."""

    def __init__(self, exc=None):
        self.exc = exc or ModelError("upstream 503")
        self.calls = 0

    def generate(self, prompt, **kw):
        self.calls += 1
        raise self.exc


class ReturnsIterator:
    """Returns a streaming iterator to a caller expecting a string.

    This is the exact bug that shipped last time: the fallback 'worked' and
    handed back an object nobody could use.
    """

    def generate(self, prompt, **kw):
        return iter(["chunk one", "chunk two"])


class ReturnsEmpty:
    def generate(self, prompt, **kw):
        return "   \n  "


class ReturnsNone:
    def generate(self, prompt, **kw):
        return None


def chain(*providers, **kw):
    kw.setdefault("_sleep", lambda _s: None)
    return FallbackModel(providers=list(providers), **kw)


def test_primary_serves_when_healthy():
    good = ScriptedModel(responses=["ok"])
    backup = Boom()
    fb = chain(Provider("primary", good), Provider("backup", backup))
    assert fb.generate("hi") == "ok"
    assert fb.last_provider == "primary"
    assert backup.calls == 0, "backup must not be called when primary works"


def test_falls_through_to_the_backup_when_the_primary_raises():
    fb = chain(
        Provider("primary", Boom(), attempts=1),
        Provider("backup", ScriptedModel(responses=["from backup"])),
    )
    assert fb.generate("hi") == "from backup"
    assert fb.last_provider == "backup"


def test_a_provider_returning_an_iterator_is_a_failure_not_a_success():
    fb = chain(
        Provider("streamer", ReturnsIterator(), attempts=1),
        Provider("backup", ScriptedModel(responses=["real text"])),
    )
    assert fb.generate("hi") == "real text"
    assert fb.last_provider == "backup"
    assert "expected str" in fb.log[0].error


def test_empty_text_is_a_failure():
    fb = chain(
        Provider("blank", ReturnsEmpty(), attempts=1),
        Provider("backup", ScriptedModel(responses=["real text"])),
    )
    assert fb.generate("hi") == "real text"
    assert "empty text" in fb.log[0].error


def test_none_is_a_failure():
    fb = chain(
        Provider("none", ReturnsNone(), attempts=1),
        Provider("backup", ScriptedModel(responses=["real text"])),
    )
    assert fb.generate("hi") == "real text"
    assert "NoneType" in fb.log[0].error


def test_retries_within_a_provider_before_moving_on():
    flaky = Boom()
    fb = chain(
        Provider("flaky", flaky, attempts=3),
        Provider("backup", ScriptedModel(responses=["ok"])),
    )
    assert fb.generate("hi") == "ok"
    assert flaky.calls == 3, "should exhaust its retries before failing over"


def test_every_provider_attempt_passes_through_the_pre_call_hook():
    called = []
    fb = chain(
        Provider("primary", Boom(), attempts=2),
        Provider("backup", ScriptedModel(responses=["ok"]), attempts=1),
        before_attempt=called.append,
    )

    assert fb.generate("hi") == "ok"
    assert called == ["primary", "primary", "backup"]


def test_backoff_grows_and_is_capped():
    delays = []
    fb = FallbackModel(
        providers=[Provider("flaky", Boom(), attempts=5)],
        backoff_s=1.0,
        max_backoff_s=4.0,
        _sleep=delays.append,
    )
    with pytest.raises(AllProvidersFailed):
        fb.generate("hi")
    assert delays == [1.0, 2.0, 4.0, 4.0], "exponential, then capped"


def test_all_failing_raises_with_every_error_attached():
    fb = chain(
        Provider("a", Boom(ModelError("a down")), attempts=1),
        Provider("b", Boom(ModelError("b down")), attempts=1),
    )
    with pytest.raises(AllProvidersFailed) as exc:
        fb.generate("hi")
    assert len(exc.value.attempts) == 2
    assert "a down" in str(exc.value) and "b down" in str(exc.value)


def test_third_provider_is_reached():
    fb = chain(
        Provider("a", Boom(), attempts=1),
        Provider("b", ReturnsIterator(), attempts=1),
        Provider("c", ScriptedModel(responses=["third"])),
    )
    assert fb.generate("hi") == "third"
    assert [a.provider for a in fb.log] == ["a", "b", "c"]


def test_an_unexpected_exception_type_still_fails_over():
    class Weird:
        def generate(self, prompt, **kw):
            raise KeyError("candidates")  # the old parse-a-missing-field bug

    fb = chain(
        Provider("weird", Weird(), attempts=1),
        Provider("backup", ScriptedModel(responses=["ok"])),
    )
    assert fb.generate("hi") == "ok"
    assert "KeyError" in fb.log[0].error


def test_arguments_reach_the_provider_that_serves():
    scripted = ScriptedModel(responses=["ok"])
    fb = chain(Provider("primary", scripted))
    fb.generate("the prompt", system="be terse", temperature=0.7)
    assert scripted.calls[0]["prompt"] == "the prompt"
    assert scripted.calls[0]["system"] == "be terse"


def test_an_empty_chain_is_rejected_at_construction():
    with pytest.raises(ValueError, match="at least one provider"):
        FallbackModel(providers=[])


def test_attempts_must_be_positive():
    with pytest.raises(ValueError, match="at least 1"):
        Provider("x", ScriptedModel(), attempts=0)


def test_last_provider_is_none_before_any_success():
    fb = chain(Provider("a", Boom(), attempts=1))
    with pytest.raises(AllProvidersFailed):
        fb.generate("hi")
    assert fb.last_provider is None


# ------------------------------------------------------------------ quota
# A provider that has run out of quota keeps none of its remaining attempts,
# because its refusal names a retry delay this backoff cannot reach. A 503 is
# the deliberate counter-case: it looks the same and is not, so it keeps them.
# Both bodies are the ones google-genai actually produced on 2026-09-07.


GEMINI_429 = ModelError(
    "gemini-3.7-flash call failed: 429 RESOURCE_EXHAUSTED. {'error': "
    "{'code': 429, 'message': 'You exceeded your current quota', "
    "'status': 'RESOURCE_EXHAUSTED', 'details': [{'retryDelay': '44s'}]}}"
)

GEMINI_503 = ModelError(
    "gemini-3.7-flash call failed: 503 UNAVAILABLE. {'error': {'code': 503, "
    "'message': 'This model is currently experiencing high demand.', "
    "'status': 'UNAVAILABLE'}}"
)


def test_an_exhausted_provider_loses_its_remaining_attempts():
    down = Boom(GEMINI_429)
    fb = chain(
        Provider("primary", down, attempts=2),
        Provider("backup", ScriptedModel(responses=["ok"])),
    )
    assert fb.generate("hi") == "ok"
    assert down.calls == 1, "the second ask cannot help and spends the quota"
    assert fb.last_provider == "backup"


def test_the_refused_attempt_is_still_recorded_and_still_reported():
    """Skipping the retry must not skip the honesty: the attempt is a fact."""
    fb = chain(
        Provider("primary", Boom(GEMINI_429), attempts=2),
        Provider("backup", ScriptedModel(responses=["ok"])),
    )
    fb.generate("hi")
    assert [(a.provider, a.ok) for a in fb.log] == [
        ("primary", False),
        ("backup", True),
    ]
    assert "RESOURCE_EXHAUSTED" in (fb.log[0].error or "")


def test_no_backoff_is_slept_for_a_provider_we_are_done_with():
    delays = []
    fb = FallbackModel(
        providers=[
            Provider("primary", Boom(GEMINI_429), attempts=3),
            Provider("backup", ScriptedModel(responses=["ok"])),
        ],
        _sleep=delays.append,
    )
    fb.generate("hi")
    assert delays == [], "the wait was for the provider we just gave up on"


def test_a_busy_provider_keeps_its_retries():
    """503 is transient load, not a wall: measured, the retry works ~half the
    time, and the alternative is answering from a weaker model."""
    busy = Boom(GEMINI_503)
    fb = chain(
        Provider("primary", busy, attempts=2),
        Provider("backup", ScriptedModel(responses=["ok"])),
    )
    assert fb.generate("hi") == "ok"
    assert busy.calls == 2


def test_a_request_level_failure_still_gets_its_retries():
    """The other half of the rule: a flake is about this call, so ask again."""
    flaky = Boom(ModelError("gemini-3.7-flash call failed: read timed out"))
    fb = chain(
        Provider("flaky", flaky, attempts=3),
        Provider("backup", ScriptedModel(responses=["ok"])),
    )
    assert fb.generate("hi") == "ok"
    assert flaky.calls == 3


def test_every_provider_can_be_exhausted_and_all_of_them_are_reported():
    fb = chain(
        Provider("a", Boom(GEMINI_429), attempts=2),
        Provider("b", Boom(GEMINI_429), attempts=2),
    )
    with pytest.raises(AllProvidersFailed) as exc:
        fb.generate("hi")
    assert len(exc.value.attempts) == 2, "one attempt each, not two"
    assert [a.provider for a in exc.value.attempts] == ["a", "b"]
    assert "RESOURCE_EXHAUSTED" in str(exc.value)


def test_provider_is_down_reads_the_status_word_not_a_bare_number():
    assert provider_is_down("429 RESOURCE_EXHAUSTED")
    # "429" alone is not enough: it turns up in URLs, part numbers and nested
    # message bodies that have nothing to do with quota.
    assert not provider_is_down("upstream 429")
    assert not provider_is_down("503 UNAVAILABLE")
    assert not provider_is_down("read timed out")
    assert not provider_is_down("returned empty text")


# --------------------------------------------------- quota cooldown
# `provider_is_down` stops the remaining attempts of ONE call. These cover the
# next call: on a free-tier key the primary's daily cap stays exhausted for the
# rest of the day, and re-asking it costs a guaranteed-failing round trip every
# time (measured 2026-09-08: 0.18 s median of six against the exhausted
# gemini-3.7-flash cap, on a run that makes five or six model calls).


def test_a_quota_refusal_is_remembered_for_the_next_call():
    down = Boom(GEMINI_429)
    fb = FallbackModel(
        providers=[
            Provider("primary", down, attempts=2),
            Provider("backup", ScriptedModel(by_marker={"hi": "ok"})),
        ],
        _clock=lambda: 100.0,
    )
    assert fb.generate("hi") == "ok"
    assert down.calls == 1
    assert fb.generate("hi") == "ok"
    assert down.calls == 1, (
        "the second call must not re-ask a provider that just refused"
    )


def test_the_skipped_provider_is_recorded_rather_than_hidden():
    """A provider nobody asked must not read like one that answered."""
    fb = FallbackModel(
        providers=[
            Provider("primary", Boom(GEMINI_429), attempts=2),
            Provider("backup", ScriptedModel(by_marker={"hi": "ok"})),
        ],
        _clock=lambda: 100.0,
    )
    fb.generate("hi")
    fb.generate("hi")
    skipped = [
        a for a in fb.log if a.provider == "primary" and "skipped" in (a.error or "")
    ]
    assert len(skipped) == 1
    assert skipped[0].ok is False
    assert skipped[0].elapsed_s == 0.0
    assert "before it is asked again" in (skipped[0].error or "")


def test_the_cooldown_expires_and_the_provider_is_probed_again():
    """A daily cap does reset. The cooldown must not park a provider forever."""
    down = Boom(GEMINI_429)
    now = [100.0]
    fb = FallbackModel(
        providers=[
            Provider("primary", down, attempts=2),
            Provider("backup", ScriptedModel(by_marker={"hi": "ok"})),
        ],
        _clock=lambda: now[0],
    )
    fb.generate("hi")
    assert down.calls == 1
    now[0] += 10.0  # inside the 44 s the refusal asked for
    fb.generate("hi")
    assert down.calls == 1
    now[0] += 100.0  # past it
    fb.generate("hi")
    assert down.calls == 2, "past the delay the provider gets probed again"


def test_the_delay_comes_from_the_refusal_not_from_a_number_invented_here():
    down = Boom(GEMINI_429)  # names retryDelay 44s
    now = [0.0]
    fb = FallbackModel(
        providers=[
            Provider("primary", down, attempts=1),
            Provider("backup", ScriptedModel(by_marker={"hi": "ok"})),
        ],
        _clock=lambda: now[0],
    )
    fb.generate("hi")
    assert fb._cooldown["primary"] == pytest.approx(44.0)


def test_a_refusal_naming_no_delay_falls_back_to_the_default():
    fb = FallbackModel(
        providers=[
            Provider("primary", Boom(ModelError("429 RESOURCE_EXHAUSTED")), attempts=1),
            Provider("backup", ScriptedModel(by_marker={"hi": "ok"})),
        ],
        _clock=lambda: 0.0,
    )
    fb.generate("hi")
    assert fb._cooldown["primary"] == pytest.approx(DEFAULT_QUOTA_COOLDOWN_S)


def test_a_delay_read_off_the_network_cannot_park_a_provider_indefinitely():
    """The refusal body is data from the network, so its number is clamped."""
    huge = ModelError("RESOURCE_EXHAUSTED, please retry in 99999s")
    fb = FallbackModel(
        providers=[
            Provider("primary", Boom(huge), attempts=1),
            Provider("backup", ScriptedModel(by_marker={"hi": "ok"})),
        ],
        _clock=lambda: 0.0,
    )
    fb.generate("hi")
    assert fb._cooldown["primary"] == pytest.approx(MAX_QUOTA_COOLDOWN_S)


def test_when_every_provider_is_cooling_they_are_all_tried_anyway():
    """A manufactured failure that asked nobody is worse than a slow answer."""
    a, b = Boom(GEMINI_429), Boom(GEMINI_429)
    fb = FallbackModel(
        providers=[Provider("a", a, attempts=1), Provider("b", b, attempts=1)],
        _clock=lambda: 100.0,
    )
    with pytest.raises(AllProvidersFailed):
        fb.generate("hi")
    assert (a.calls, b.calls) == (1, 1)
    with pytest.raises(AllProvidersFailed):
        fb.generate("hi")
    assert (a.calls, b.calls) == (2, 2), "all cooling means try them all, not fail dry"


def test_a_503_does_not_start_a_cooldown():
    """Only the quota wall is a wall; transient load keeps its retries."""
    busy = Boom(GEMINI_503)
    fb = FallbackModel(
        providers=[
            Provider("primary", busy, attempts=1),
            Provider("backup", ScriptedModel(by_marker={"hi": "ok"})),
        ],
        _clock=lambda: 0.0,
    )
    fb.generate("hi")
    fb.generate("hi")
    assert fb._cooldown == {}
    assert busy.calls == 2


def test_retry_delay_s_reads_both_spellings_and_refuses_nonsense():
    assert retry_delay_s("Please retry in 29.82992956s") == pytest.approx(29.82992956)
    assert retry_delay_s("'retryDelay': '44s'") == pytest.approx(44.0)
    assert retry_delay_s("no delay here") is None
    assert retry_delay_s("retry in 0s") is None

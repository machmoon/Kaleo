"""The live Gemini path, exercised for real behind an API-key gate.

Every other test in this suite runs offline against :class:`ScriptedModel`, and
CLAUDE.md promises the full suite needs no keys. This module keeps that promise:
the tests that actually call Gemini are marked ``requires_api_key`` and skip
unless ``GOOGLE_API_KEY`` is set, so a default ``pytest`` run stays offline and
free. The mark is applied per test rather than as a module-level ``pytestmark``
because the no-key construction test below must run in exactly the environment
that has no key.

When the gate is open the live surface is deliberately tiny: one call to the
cheap model asking for one small JSON object. That is enough to catch the
failures a scripted model cannot -- a renamed model id, a changed config key, an
SDK response object that no longer carries ``.text``.
"""

from __future__ import annotations

import os
import sys
from types import ModuleType, SimpleNamespace

import pytest
from silkscreen.agents.model import (
    CHEAP_MODEL,
    DEFAULT_TIMEOUT_S,
    GEMMA_MODEL,
    TIMEOUT_ENV_VAR,
    Document,
    GeminiModel,
    ModelError,
    ScriptedModel,
    request_timeout_ms,
    strip_code_fence,
)
from silkscreen.agents.resilience import FallbackModel, Provider
from silkscreen.agents.retrieval import GeminiEmbedder

requires_api_key = pytest.mark.skipif(
    not os.getenv("GOOGLE_API_KEY"),
    reason="live Gemini test: set GOOGLE_API_KEY to run",
)


# ------------------------------------------------------ no key, no network


def test_gemini_model_without_api_key_raises_model_error(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    with pytest.raises(ModelError) as excinfo:
        GeminiModel()

    assert "GOOGLE_API_KEY" in str(excinfo.value)


def test_document_request_uses_valid_high_media_resolution(monkeypatch):
    """Catch request-config errors without making a live Gemini call."""
    captured = {}

    class FakeModels:
        def generate_content(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(text="ok")

    class FakeClient:
        def __init__(self, **_kwargs):
            self.models = FakeModels()

    fake_google = ModuleType("google")
    fake_genai = ModuleType("google.genai")
    fake_genai.Client = FakeClient
    fake_google.genai = fake_genai
    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)

    model = GeminiModel(api_key="test-key")
    response = model.generate(
        "read this datasheet",
        documents=[Document(url="https://example.com/part.pdf")],
    )

    assert response == "ok"
    assert captured["config"]["media_resolution"] == "MEDIA_RESOLUTION_HIGH"


# ------------------------------------------------------------- live Gemini

#: A token the model has to echo back. Nonsense on purpose: it cannot be
#: produced by a cached, refused, or truncated answer that happens to look fine.
MARKER = "silkscreen-live-ok"

PROMPT = (
    "Reply with exactly this JSON object and nothing else: "
    f'{{"marker": "{MARKER}"}}'
)


@pytest.fixture(scope="module")
def live_response() -> str:
    """One real call to the cheap model, shared by every live test here.

    Module-scoped so the whole gated suite costs a single request. Only
    ``@requires_api_key`` tests may request it -- an ungated consumer would
    spend a real request on a machine that never asked for one.
    """
    model = GeminiModel(CHEAP_MODEL)
    return model.generate(PROMPT, temperature=0.0, max_output_tokens=512)


@requires_api_key
def test_live_generate_returns_non_empty_text(live_response):
    assert isinstance(live_response, str)
    assert live_response.strip()


@requires_api_key
def test_live_generate_returns_the_requested_marker(live_response):
    # The model may wrap JSON in a Markdown fence; parse_circuit_spec tolerates
    # that in production, so this test does too rather than failing on style.
    assert MARKER in strip_code_fence(live_response)


@requires_api_key
def test_live_gemma_rung_answers():
    """One real call to the Gemma rung of the service fallback chain.

    resilience.py's founding rule: a fallback that has never been executed is
    a second bug waiting for the first one to happen. The Gemini tiers are
    exercised by the fixture above; this is the same proof for the open-weights
    last resort, including that Gemma accepts a system instruction.
    """
    model = GeminiModel(GEMMA_MODEL)
    text = model.generate(
        PROMPT,
        system="You answer with exactly what is asked, nothing else.",
        temperature=0.0,
        max_output_tokens=512,
    )
    assert MARKER in strip_code_fence(text)


# ---------------------------------------------- request deadline, no network


class _FakeReadTimeout(Exception):
    """Shaped like httpx.ReadTimeout: what the SDK raises when the deadline
    passes with no response bytes."""


def _install_fake_genai(monkeypatch, *, raise_on_generate=None):
    """Install a fake ``google.genai`` and return the capture dicts.

    Returns ``(client_kwargs, call_kwargs)``: the kwargs the Client was
    constructed with, and the kwargs of the last generate/embed call.
    """
    client_kwargs: dict = {}
    call_kwargs: dict = {}

    class FakeModels:
        def generate_content(self, **kwargs):
            call_kwargs.update(kwargs)
            if raise_on_generate is not None:
                raise raise_on_generate
            return SimpleNamespace(text="ok")

        def embed_content(self, **kwargs):
            call_kwargs.update(kwargs)
            return SimpleNamespace(embeddings=[])

    class FakeClient:
        def __init__(self, **kwargs):
            client_kwargs.update(kwargs)
            self.models = FakeModels()

    fake_google = ModuleType("google")
    fake_genai = ModuleType("google.genai")
    fake_genai.Client = FakeClient
    fake_google.genai = fake_genai
    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.delenv(TIMEOUT_ENV_VAR, raising=False)
    return client_kwargs, call_kwargs


def test_client_carries_the_default_request_timeout(monkeypatch):
    """Every call path gets a deadline because it lives on the client itself.

    Without one the SDK waits forever, so a 504-degraded model hangs the CLI
    silently instead of raising into FallbackModel (observed 2026-08-30).
    """
    client_kwargs, _ = _install_fake_genai(monkeypatch)

    model = GeminiModel(api_key="test-key")

    # HttpOptions.timeout is milliseconds in google-genai (verified against
    # the installed SDK: it divides by 1000 before handing httpx the request).
    assert client_kwargs["http_options"] == {"timeout": 60_000}
    assert model.timeout_s == DEFAULT_TIMEOUT_S


def test_constructor_timeout_overrides_default(monkeypatch):
    client_kwargs, _ = _install_fake_genai(monkeypatch)

    model = GeminiModel(api_key="test-key", timeout_s=5)

    assert client_kwargs["http_options"] == {"timeout": 5_000}
    assert model.timeout_s == 5.0


def test_env_var_overrides_default_and_constructor_wins(monkeypatch):
    client_kwargs, _ = _install_fake_genai(monkeypatch)
    monkeypatch.setenv(TIMEOUT_ENV_VAR, "90")

    GeminiModel(api_key="test-key")
    assert client_kwargs["http_options"] == {"timeout": 90_000}

    GeminiModel(api_key="test-key", timeout_s=2.5)
    assert client_kwargs["http_options"] == {"timeout": 2_500}


@pytest.mark.parametrize("raw", ["soon", "", "  ", "0", "-3", "inf", "nan"])
def test_bad_timeout_values_raise_instead_of_hanging_silently(monkeypatch, raw):
    """A typo that silently dropped the deadline would recreate the hang."""
    _install_fake_genai(monkeypatch)
    monkeypatch.setenv(TIMEOUT_ENV_VAR, raw)

    if raw.strip() == "":
        # Empty means unset, not broken: the default applies.
        assert request_timeout_ms() == 60_000
        return

    with pytest.raises(ModelError):
        GeminiModel(api_key="test-key")


def test_timeout_exception_is_a_model_error(monkeypatch):
    _install_fake_genai(
        monkeypatch, raise_on_generate=_FakeReadTimeout("deadline exceeded")
    )
    model = GeminiModel(api_key="test-key")

    with pytest.raises(ModelError):
        model.generate("hello")


def test_timeout_fails_over_to_the_backup_provider(monkeypatch):
    """The point of the deadline: a hung primary becomes a failover, not a hang."""
    _install_fake_genai(
        monkeypatch, raise_on_generate=_FakeReadTimeout("deadline exceeded")
    )
    primary = GeminiModel(api_key="test-key")
    backup = ScriptedModel(responses=["rescued"])
    fb = FallbackModel(
        providers=[
            Provider("primary", primary, attempts=1),
            Provider("backup", backup, attempts=1),
        ],
        _sleep=lambda _s: None,
    )

    assert fb.generate("hello") == "rescued"
    assert fb.last_provider == "backup"
    assert not fb.log[0].ok
    assert "ModelError" in fb.log[0].error


def test_embedder_client_carries_the_same_timeout(monkeypatch):
    """GeminiEmbedder builds its own client, so it needs its own deadline."""
    client_kwargs, _ = _install_fake_genai(monkeypatch)

    embedder = GeminiEmbedder(api_key="test-key")

    assert client_kwargs["http_options"] == {"timeout": 60_000}
    assert embedder.timeout_s == DEFAULT_TIMEOUT_S


def test_real_sdk_accepts_the_timeout_shape(monkeypatch):
    """Construct against the installed google-genai, no network involved.

    Guards the SDK contract the fakes above assume: Client accepts an
    ``http_options`` dict and stores the millisecond timeout. Construction
    makes no request, so this runs ungated.
    """
    monkeypatch.delenv(TIMEOUT_ENV_VAR, raising=False)

    model = GeminiModel(api_key="offline-test-key")

    stored = model._client._api_client._http_options.timeout
    assert stored == 60_000

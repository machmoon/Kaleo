"""Model discovery is dynamic, but selection stays server-controlled."""

import pytest

from service import models


def test_catalog_caches_the_live_discovery(monkeypatch):
    found = {
        "default": "auto",
        "auto_model": "gemini-a",
        "source": "gemini",
        "models": [{"id": "gemini-a"}],
    }
    calls = []
    monkeypatch.setattr(models, "_cache", None)
    monkeypatch.setattr(models, "_live_catalog", lambda: calls.append(1) or found)

    assert models.model_catalog() is found
    assert models.model_catalog() is found
    assert calls == [1]


def test_auto_and_an_advertised_model_are_selectable():
    catalog = {
        "auto_model": "gemini-auto",
        "models": [{"id": "gemini-auto"}, {"id": "gemini-debug"}],
    }

    assert models.select_model("auto", catalog) == "gemini-auto"
    assert models.select_model("gemini-debug", catalog) == "gemini-debug"


def test_an_arbitrary_model_id_is_rejected():
    catalog = {"auto_model": "gemini-auto", "models": [{"id": "gemini-auto"}]}

    try:
        models.select_model("gemini-invented", catalog)
    except ValueError as exc:
        assert "available Gemini model" in str(exc)
    else:  # pragma: no cover - the assertion above is the contract
        raise AssertionError("an unadvertised model was accepted")


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        (None, None),
        ("", None),
        ("auto", None),
        ("LOW", "low"),
        ("medium", "medium"),
        ("high", "high"),
    ],
)
def test_thinking_level_is_normalized(requested, expected):
    assert models.select_thinking_level(requested) == expected


def test_an_unsupported_thinking_level_is_rejected():
    with pytest.raises(ValueError, match="thinking_level"):
        models.select_thinking_level("off")


@pytest.mark.parametrize(
    ("requested", "expected"),
    [(None, None), ("auto", None), (3, 3), ("6", 6), (15, 15)],
)
def test_quota_rpm_is_normalized(requested, expected):
    assert models.select_quota_rpm(requested) == expected


@pytest.mark.parametrize("requested", [True, 0, 4, 20, "6.0", "fast"])
def test_an_unsupported_quota_rpm_is_rejected(requested):
    with pytest.raises(ValueError, match="quota_rpm"):
        models.select_quota_rpm(requested)


def test_fallback_catalog_keeps_both_demo_orchestrators_selectable():
    catalog = models._fallback("offline")
    ids = {item["id"] for item in catalog["models"]}

    assert {"gemini-3.7-flash", "gemini-3.1-pro-preview"} <= ids


# -- the Gemini 3.5 floor on the selectable root model ------------------------


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("gemini-3.7-flash", True),
        ("gemini-3.5-flash-lite", True),
        ("gemini-3.1-pro-preview", False),
        ("gemini-2.5-flash", False),
        ("gemini-10.0-ultra", True),
        ("gemini-auto", True),
    ],
)
def test_the_version_floor_reads_major_and_minor(model_id, expected):
    assert models.meets_version_floor(model_id) is expected


def test_a_model_below_the_floor_is_refused_even_when_advertised(monkeypatch):
    monkeypatch.delenv("SILKSCREEN_ALLOW_LEGACY_MODELS", raising=False)
    catalog = models._fallback("offline")

    assert models.select_model("gemini-3.7-flash", catalog) == "gemini-3.7-flash"
    with pytest.raises(ValueError, match="3.5 floor"):
        models.select_model("gemini-3.1-pro-preview", catalog)


def test_the_operator_can_opt_a_legacy_model_back_in(monkeypatch):
    monkeypatch.setenv("SILKSCREEN_ALLOW_LEGACY_MODELS", "1")
    catalog = models._fallback("offline")

    chosen = models.select_model("gemini-3.1-pro-preview", catalog)

    assert chosen == "gemini-3.1-pro-preview"


def test_the_catalog_marks_models_below_the_floor_as_legacy():
    by_id = {item["id"]: item for item in models._fallback("offline")["models"]}

    assert by_id["gemini-3.7-flash"]["legacy"] is False
    assert by_id["gemini-3.1-pro-preview"]["legacy"] is True

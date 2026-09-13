"""Gemini model discovery for the same-origin web client.

The Gemini API is the authority on what the current key may call.  A small
fallback catalog keeps the UI useful before a key is configured and during a
transient discovery failure; it is a fallback, not a claim that those models
are currently reachable.
"""

from __future__ import annotations

import os
import re
import threading
import time
from typing import Any

from silkscreen.agents.model import CHEAP_MODEL, DEFAULT_MODEL

__all__ = [
    "meets_version_floor",
    "model_catalog",
    "select_model",
    "select_quota_rpm",
    "select_thinking_level",
]

QUOTA_RPM_OPTIONS = frozenset({3, 6, 15})

_CACHE_TTL_S = 15 * 60
_cache_lock = threading.Lock()
_cache_at = 0.0
_cache: dict[str, Any] | None = None
_PRO_ORCHESTRATOR_MODEL = "gemini-3.1-pro-preview"

#: The hackathon's model floor: Gemini 3.5 or newer. The worker chain meets it
#: by construction (``DEFAULT_MODEL``/``CHEAP_MODEL``), but the root model is
#: user-selectable, so the floor is enforced where the selection is made. A
#: model below it is still listed -- marked ``legacy`` -- because hiding it
#: would make a stale catalog look like a discovery failure.
_ROOT_VERSION_FLOOR = (3, 5)
_LEGACY_OPT_IN = "SILKSCREEN_ALLOW_LEGACY_MODELS"
_VERSION = re.compile(r"^gemini-(\d+)\.(\d+)")


def _version(model_id: str) -> tuple[int, int] | None:
    match = _VERSION.match(model_id)
    return (int(match[1]), int(match[2])) if match else None


def meets_version_floor(model_id: str) -> bool:
    """Is this model at or above the Gemini 3.5 floor?

    An id that carries no ``gemini-<major>.<minor>`` prefix cannot be
    classified and is passed through: the catalog only ever lists
    ``generateContent`` Gemini models, and refusing an unfamiliar id would
    turn a naming change on Google's side into an outage here.
    """
    version = _version(model_id)
    return version is None or version >= _ROOT_VERSION_FLOOR


def _legacy_allowed() -> bool:
    return os.getenv(_LEGACY_OPT_IN, "").strip() == "1"


def _fallback(reason: str = "") -> dict[str, Any]:
    models = []
    for model_id, label in (
        (DEFAULT_MODEL, "Default Gemini model"),
        (_PRO_ORCHESTRATOR_MODEL, "Reasoning Gemini orchestrator"),
        (CHEAP_MODEL, "Economy Gemini model"),
    ):
        if any(item["id"] == model_id for item in models):
            continue
        models.append(
            {
                "id": model_id,
                "name": label,
                "description": "Configured by the Silkscreen service.",
                "input_token_limit": None,
                "output_token_limit": None,
                "thinking": None,
                "legacy": not meets_version_floor(model_id),
            }
        )
    return {
        "default": "auto",
        "auto_model": os.getenv("SILKSCREEN_ORCHESTRATOR_MODEL") or DEFAULT_MODEL,
        "source": "fallback",
        "models": models,
        **({"warning": reason} if reason else {}),
    }


def _clean_name(value: object) -> str:
    name = str(value or "")
    return name.removeprefix("models/")


def _live_catalog() -> dict[str, Any]:
    from google import genai

    key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not key:
        return _fallback("Gemini model discovery needs GOOGLE_API_KEY.")

    client = genai.Client(api_key=key)
    try:
        models = []
        for raw in client.models.list():
            actions = list(getattr(raw, "supported_actions", None) or [])
            if "generateContent" not in actions:
                continue
            model_id = _clean_name(getattr(raw, "name", ""))
            if not model_id.startswith("gemini-"):
                continue
            models.append(
                {
                    "id": model_id,
                    "name": str(getattr(raw, "display_name", None) or model_id),
                    "description": str(getattr(raw, "description", None) or ""),
                    "input_token_limit": getattr(raw, "input_token_limit", None),
                    "output_token_limit": getattr(raw, "output_token_limit", None),
                    "thinking": getattr(raw, "thinking", None),
                    "legacy": not meets_version_floor(model_id),
                }
            )
    finally:
        client.close()

    models.sort(key=lambda item: item["id"])
    if not models:
        return _fallback("Gemini returned no generateContent models.")
    auto_model = os.getenv("SILKSCREEN_ORCHESTRATOR_MODEL") or DEFAULT_MODEL
    return {
        "default": "auto",
        "auto_model": auto_model,
        "source": "gemini",
        "models": models,
    }


def model_catalog(*, refresh: bool = False) -> dict[str, Any]:
    """Available text-generation models, cached briefly per service process."""
    global _cache, _cache_at

    now = time.monotonic()
    with _cache_lock:
        if not refresh and _cache is not None and now - _cache_at < _CACHE_TTL_S:
            return _cache
        try:
            catalog = _live_catalog()
        except Exception as exc:  # discovery must never take the UI down
            catalog = _fallback(f"Model discovery failed: {type(exc).__name__}: {exc}")
        _cache = catalog
        _cache_at = now
        return catalog


def select_model(requested: object, catalog: dict[str, Any]) -> str:
    """Resolve ``auto`` or require a model advertised by this server."""
    choice = str(requested or "auto").strip()
    if not choice or choice == "auto":
        return str(catalog.get("auto_model") or DEFAULT_MODEL)
    allowed = {str(item.get("id") or "") for item in catalog.get("models", [])}
    if choice not in allowed:
        raise ValueError("'model' must be 'auto' or an available Gemini model")
    if not meets_version_floor(choice) and not _legacy_allowed():
        raise ValueError(
            f"'model' {choice} is below the Gemini 3.5 floor; choose 'auto' or a "
            f"newer model, or set {_LEGACY_OPT_IN}=1 on the service to allow it"
        )
    return choice


def select_thinking_level(requested: object) -> str | None:
    """Resolve the web control to a Gemini 3 thinking level.

    Gemini 3.1 Pro and 3.7 Flash cannot turn thinking fully off.  ``auto``
    therefore means the selected model's native default, while explicit
    choices use the three levels both models support.
    """
    choice = str(requested or "auto").strip().lower()
    if not choice or choice == "auto":
        return None
    if choice not in {"low", "medium", "high"}:
        raise ValueError("'thinking_level' must be 'auto', 'low', 'medium', or 'high'")
    return choice


def select_quota_rpm(requested: object) -> int | None:
    """Return the selected app-side request pace; ``None`` means no pacing."""
    if requested is None or requested == "":
        return None
    if isinstance(requested, str) and requested.strip().lower() == "auto":
        return None
    if isinstance(requested, bool):
        raise ValueError("'quota_rpm' must be 'auto', 3, 6, or 15")
    try:
        rpm = int(requested)
    except (TypeError, ValueError) as exc:
        raise ValueError("'quota_rpm' must be 'auto', 3, 6, or 15") from exc
    if str(requested).strip() != str(rpm) or rpm not in QUOTA_RPM_OPTIONS:
        raise ValueError("'quota_rpm' must be 'auto', 3, 6, or 15")
    return rpm

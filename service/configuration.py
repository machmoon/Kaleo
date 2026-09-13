"""Secret-safe runtime readiness for the web configuration monitor.

The service reads configuration from ``os.environ``.  ``silkscreen serve``
loads the repository's ``.env`` before importing the service, while Cloud Run
injects environment variables directly.  This module reports both truths:
what the running process can use now, and whether a local ``.env`` differs
from that process and therefore needs a restart.

No configuration value is returned to the browser.  Only feature names,
variable names, readiness states, and human-readable remediation are exposed.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .models import model_catalog

__all__ = ["configuration_status", "dotenv_fingerprint", "probe_ollama"]

REPO_ROOT = Path(__file__).resolve().parent.parent
DOTENV_PATH = REPO_ROOT / ".env"
OLLAMA_TIMEOUT_S = 1.5
MAX_OLLAMA_RESPONSE_BYTES = 1 << 20

# Keep this list aligned with .env.example plus accepted compatibility and
# service-only variables.  Names are safe to disclose; their values are not.
_MONITORED_KEYS = frozenset(
    {
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_CLOUD_PROJECT",
        "OLLAMA_PLACEMENT_MODEL",
        "OLLAMA_PLACEMENT_URL",
        "PLACEMENT_FAILURE_TRACE_PATH",
        "SILKSCREEN_ENGINE",
        "SILKSCREEN_ORCHESTRATOR_MODEL",
        "SILKSCREEN_WEB_DIST",
        "TINKER_API_KEY",
        "TINKER_PLACEMENT_MODEL",
        "USE_FIRESTORE",
    }
)


def dotenv_fingerprint(path: Path = DOTENV_PATH) -> str:
    """Return an opaque in-process fingerprint, or an empty string if absent."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


# Imported after ``silkscreen serve`` loaded .env.  A later edit changes this
# fingerprint but cannot change os.environ, which is exactly what the monitor
# needs to explain.
_STARTUP_DOTENV_FINGERPRINT = dotenv_fingerprint()


def _read_dotenv(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key in _MONITORED_KEYS:
            values[key] = value.strip().strip('"').strip("'")
    return values


def _installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, AttributeError, ValueError):
        return False


def _feature(
    feature_id: str,
    label: str,
    state: str,
    summary: str,
    variables: list[str],
) -> dict[str, Any]:
    return {
        "id": feature_id,
        "label": label,
        "state": state,
        "summary": summary,
        "variables": variables,
    }


def probe_ollama(base_url: str, model: str) -> tuple[str, str]:
    """Check that Ollama answers and advertises the configured local model."""
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "error", "OLLAMA_PLACEMENT_URL must be an HTTP or HTTPS URL."

    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/tags",
        headers={"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=OLLAMA_TIMEOUT_S) as response:
            payload = json.loads(response.read(MAX_OLLAMA_RESPONSE_BYTES))
    except Exception:
        return (
            "error",
            "Configured endpoint is not answering; start Ollama and try again.",
        )

    models = payload.get("models", []) if isinstance(payload, dict) else []
    names = {
        str(item.get("name") or item.get("model") or "")
        for item in models
        if isinstance(item, dict)
    }
    if model not in names:
        return "error", f"Ollama is online, but {model} is not installed."
    return "ready", f"Ollama is online and {model} is installed."


def configuration_status(
    *,
    environ: Mapping[str, str] | None = None,
    dotenv_path: Path = DOTENV_PATH,
    startup_fingerprint: str = _STARTUP_DOTENV_FINGERPRINT,
    catalog_factory: Callable[[], dict[str, Any]] = model_catalog,
    ollama_probe: Callable[[str, str], tuple[str, str]] = probe_ollama,
    installed: Callable[[str], bool] = _installed,
) -> dict[str, Any]:
    """Describe active feature readiness without returning configuration values."""
    active = os.environ if environ is None else environ
    dotenv = _read_dotenv(dotenv_path)
    present = dotenv_path.is_file()
    current_fingerprint = dotenv_fingerprint(dotenv_path)
    changed_since_start = current_fingerprint != startup_fingerprint
    pending = sorted(
        key
        for key, file_value in dotenv.items()
        if file_value != str(active.get(key, ""))
    )
    reload_required = bool(pending)

    if reload_required:
        dotenv_state = "restart"
        dotenv_summary = (
            f"The running backend differs from .env in {len(pending)} "
            f"setting{'s' if len(pending) != 1 else ''}; restart to apply the file."
        )
    elif present:
        dotenv_state = "ready"
        dotenv_summary = ".env matches the running backend."
    else:
        dotenv_state = "off"
        dotenv_summary = "No .env file; using the deployed process environment."

    features: list[dict[str, Any]] = []

    engine = str(active.get("SILKSCREEN_ENGINE", "")).strip().lower() or "adk"
    if engine not in {"adk", "sdk"}:
        features.append(
            _feature(
                "engine",
                "Pipeline engine",
                "error",
                "SILKSCREEN_ENGINE must be adk or sdk.",
                ["SILKSCREEN_ENGINE"],
            )
        )
    elif engine == "adk" and not installed("google.adk"):
        features.append(
            _feature(
                "engine",
                "Pipeline engine",
                "error",
                "Google ADK is selected but the adk dependency is not installed.",
                ["SILKSCREEN_ENGINE"],
            )
        )
    else:
        label = "Google ADK" if engine == "adk" else "SDK fallback"
        features.append(
            _feature(
                "engine",
                "Pipeline engine",
                "ready",
                f"{label} is selected and installed.",
                ["SILKSCREEN_ENGINE"],
            )
        )

    gemini_key = str(
        active.get("GOOGLE_API_KEY", "") or active.get("GEMINI_API_KEY", "")
    ).strip()
    dotenv_gemini_key = str(
        dotenv.get("GOOGLE_API_KEY", "") or dotenv.get("GEMINI_API_KEY", "")
    ).strip()
    if not gemini_key:
        if dotenv_gemini_key:
            gemini_state = "restart"
            gemini_summary = (
                "A Gemini key is in .env but is not loaded; restart the backend."
            )
        else:
            gemini_state = "error"
            gemini_summary = "Add GOOGLE_API_KEY to run the orchestrator and workers."
    else:
        catalog = catalog_factory()
        if catalog.get("source") == "gemini":
            count = len(catalog.get("models", []))
            gemini_state = "ready"
            suffix = "s" if count != 1 else ""
            gemini_summary = (
                f"API access verified; {count} generation model{suffix} available."
            )
        else:
            gemini_state = "warning"
            gemini_summary = (
                "Key loaded, but live model discovery could not be verified."
            )
    features.append(
        _feature(
            "gemini",
            "Gemini agents",
            gemini_state,
            gemini_summary,
            ["GOOGLE_API_KEY"],
        )
    )

    ollama_url = str(active.get("OLLAMA_PLACEMENT_URL", "")).strip()
    ollama_model = str(active.get("OLLAMA_PLACEMENT_MODEL", "")).strip() or "gemma3:4b"
    if not ollama_url:
        if str(dotenv.get("OLLAMA_PLACEMENT_URL", "")).strip():
            ollama_state = "restart"
            ollama_summary = (
                "The Ollama URL is in .env but is not loaded; restart the backend."
            )
        else:
            ollama_state = "off"
            ollama_summary = (
                "Optional; set a URL and pull a model. No API key is required."
            )
    else:
        ollama_state, ollama_summary = ollama_probe(ollama_url, ollama_model)
    features.append(
        _feature(
            "ollama",
            "Ollama placement",
            ollama_state,
            ollama_summary,
            ["OLLAMA_PLACEMENT_URL", "OLLAMA_PLACEMENT_MODEL"],
        )
    )

    tinker_key = str(active.get("TINKER_API_KEY", "")).strip()
    tinker_model = str(active.get("TINKER_PLACEMENT_MODEL", "")).strip()
    file_tinker_ready = bool(
        str(dotenv.get("TINKER_API_KEY", "")).strip()
        and str(dotenv.get("TINKER_PLACEMENT_MODEL", "")).strip()
    )
    if not tinker_key and not tinker_model:
        if file_tinker_ready:
            tinker_state = "restart"
            tinker_summary = (
                "Tinker settings are in .env but are not loaded; restart the backend."
            )
        else:
            tinker_state = "off"
            tinker_summary = (
                "Optional; requires an API key and promoted tinker:// checkpoint."
            )
    elif not tinker_key or not tinker_model:
        missing = "TINKER_API_KEY" if not tinker_key else "TINKER_PLACEMENT_MODEL"
        tinker_state = "error"
        tinker_summary = f"Incomplete configuration; add {missing}."
    elif not tinker_model.startswith("tinker://"):
        tinker_state = "error"
        tinker_summary = (
            "TINKER_PLACEMENT_MODEL must be a promoted tinker:// checkpoint."
        )
    elif not installed("tinker"):
        tinker_state = "error"
        tinker_summary = (
            "Tinker is configured, but the training dependency is not installed."
        )
    else:
        tinker_state = "ready"
        tinker_summary = (
            "Key, promoted checkpoint, and training dependency are available."
        )
    features.append(
        _feature(
            "tinker",
            "Tinker placement",
            tinker_state,
            tinker_summary,
            ["TINKER_API_KEY", "TINKER_PLACEMENT_MODEL"],
        )
    )

    project = str(active.get("GOOGLE_CLOUD_PROJECT", "")).strip()
    firestore_enabled = str(active.get("USE_FIRESTORE", "1")).strip() != "0"
    if not project or not firestore_enabled:
        firestore_state = "off"
        firestore_summary = "Optional; the in-memory datasheet cache is active."
    elif not installed("google.cloud.firestore"):
        firestore_state = "error"
        firestore_summary = (
            "Cloud cache is selected, but the cloud dependency is not installed."
        )
    else:
        firestore_state = "ready"
        firestore_summary = "Firestore datasheet caching is configured."
    features.append(
        _feature(
            "firestore",
            "Datasheet cache",
            firestore_state,
            firestore_summary,
            ["GOOGLE_CLOUD_PROJECT", "USE_FIRESTORE"],
        )
    )

    return {
        "version": 1,
        "dotenv": {
            "present": present,
            "state": dotenv_state,
            "summary": dotenv_summary,
            "reload_required": reload_required,
            "changed_since_start": changed_since_start,
            "pending": pending,
        },
        "features": features,
    }

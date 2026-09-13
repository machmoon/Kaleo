"""Runtime configuration readiness stays useful without exposing secrets."""

from __future__ import annotations

import json

from service.configuration import configuration_status, dotenv_fingerprint, probe_ollama


def _feature(status, feature_id):
    return next(item for item in status["features"] if item["id"] == feature_id)


def test_missing_optional_providers_are_off_but_gemini_is_actionable(tmp_path):
    status = configuration_status(
        environ={},
        dotenv_path=tmp_path / ".env",
        startup_fingerprint="",
        catalog_factory=lambda: (_ for _ in ()).throw(
            AssertionError("catalog must not run without a key")
        ),
        installed=lambda _module: True,
    )

    assert status["dotenv"]["state"] == "off"
    assert _feature(status, "engine")["state"] == "ready"
    assert _feature(status, "gemini")["state"] == "error"
    assert _feature(status, "ollama")["state"] == "off"
    assert _feature(status, "tinker")["state"] == "off"
    assert _feature(status, "firestore")["state"] == "off"


def test_dotenv_change_reports_restart_without_returning_values(tmp_path):
    dotenv = tmp_path / ".env"
    secret = "do-not-return-this-secret"
    dotenv.write_text(f"GOOGLE_API_KEY={secret}\n", encoding="utf-8")

    status = configuration_status(
        environ={},
        dotenv_path=dotenv,
        startup_fingerprint="",
        catalog_factory=lambda: {},
        installed=lambda _module: True,
    )

    assert status["dotenv"] == {
        "present": True,
        "state": "restart",
        "summary": (
            "The running backend differs from .env in 1 setting; "
            "restart to apply the file."
        ),
        "reload_required": True,
        "changed_since_start": True,
        "pending": ["GOOGLE_API_KEY"],
    }
    assert _feature(status, "gemini")["state"] == "restart"
    assert secret not in json.dumps(status)


def test_configured_features_report_active_readiness(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "\n".join(
            [
                "GOOGLE_API_KEY=test-key",
                "SILKSCREEN_ENGINE=adk",
                "OLLAMA_PLACEMENT_URL=http://127.0.0.1:11434",
                "OLLAMA_PLACEMENT_MODEL=gemma3:4b",
                "TINKER_API_KEY=tinker-key",
                "TINKER_PLACEMENT_MODEL=tinker://run/checkpoint",
                "GOOGLE_CLOUD_PROJECT=demo-project",
            ]
        ),
        encoding="utf-8",
    )
    environ = {
        "GOOGLE_API_KEY": "test-key",
        "SILKSCREEN_ENGINE": "adk",
        "OLLAMA_PLACEMENT_URL": "http://127.0.0.1:11434",
        "OLLAMA_PLACEMENT_MODEL": "gemma3:4b",
        "TINKER_API_KEY": "tinker-key",
        "TINKER_PLACEMENT_MODEL": "tinker://run/checkpoint",
        "GOOGLE_CLOUD_PROJECT": "demo-project",
    }
    probes = []

    def ollama(url, model):
        probes.append((url, model))
        return "ready", "local model ready"

    status = configuration_status(
        environ=environ,
        dotenv_path=dotenv,
        startup_fingerprint=dotenv_fingerprint(dotenv),
        catalog_factory=lambda: {
            "source": "gemini",
            "models": [{"id": "gemini-test"}],
        },
        ollama_probe=ollama,
        installed=lambda _module: True,
    )

    assert status["dotenv"]["state"] == "ready"
    assert status["dotenv"]["changed_since_start"] is False
    assert probes == [("http://127.0.0.1:11434", "gemma3:4b")]
    assert {item["state"] for item in status["features"]} == {"ready"}


def test_ollama_url_is_validated_before_a_network_call():
    state, summary = probe_ollama("file:///tmp/ollama", "gemma3:4b")

    assert state == "error"
    assert "HTTP or HTTPS" in summary


def test_ollama_probe_checks_the_configured_model(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return json.dumps({"models": [{"name": "gemma3:4b"}]}).encode()

    seen = []

    def open_request(request, *, timeout):
        seen.append((request.full_url, timeout))
        return Response()

    monkeypatch.setattr("service.configuration.urllib.request.urlopen", open_request)

    state, summary = probe_ollama("http://127.0.0.1:11434/", "gemma3:4b")

    assert state == "ready"
    assert "installed" in summary
    assert seen == [("http://127.0.0.1:11434/api/tags", 1.5)]

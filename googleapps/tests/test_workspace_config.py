"""Configuration: per-command requirements, and never echoing a secret."""

from __future__ import annotations

from pathlib import Path

import pytest

from googleapps.config import DEFAULT_TOKEN_PATH, ConfigError, load_config

FULL = {
    "GOOGLEAPPS_CLIENT_ID": "123.apps.googleusercontent.com",
    "GOOGLEAPPS_CLIENT_SECRET": "GOCSPX-secret-value",
    "GOOGLEAPPS_CHAT_WEBHOOK": "https://chat.googleapis.com/v1/spaces/A/messages?key=k",
    "GOOGLE_API_KEY": "AIza-key",
}


def test_a_full_environment_loads():
    config = load_config(dict(FULL))
    assert config.client_id == "123.apps.googleusercontent.com"
    assert config.token_path == DEFAULT_TOKEN_PATH
    config.require_oauth()
    config.require_webhook()
    config.require_api_key()


def test_nothing_is_required_just_to_load():
    """`check` must be able to report an empty environment, not crash on it."""
    config = load_config({})
    assert config.client_id == ""
    assert config.chat_webhook == ""


def test_oauth_requirement_names_every_missing_variable_at_once():
    """One round of corrections, not two."""
    with pytest.raises(ConfigError) as excinfo:
        load_config({}).require_oauth()
    message = str(excinfo.value)
    assert "GOOGLEAPPS_CLIENT_ID" in message
    assert "GOOGLEAPPS_CLIENT_SECRET" in message


def test_webhook_and_api_key_requirements_name_their_variable():
    with pytest.raises(ConfigError, match="GOOGLEAPPS_CHAT_WEBHOOK"):
        load_config({}).require_webhook()
    with pytest.raises(ConfigError, match="GOOGLE_API_KEY"):
        load_config({}).require_api_key()


def test_blank_is_treated_as_missing():
    env = {**FULL, "GOOGLEAPPS_CLIENT_SECRET": "   "}
    with pytest.raises(ConfigError):
        load_config(env).require_oauth()


def test_token_path_override_and_default():
    config = load_config({**FULL, "GOOGLEAPPS_TOKEN_PATH": "/tmp/t.json"})
    assert config.token_path == Path("/tmp/t.json")
    assert load_config(dict(FULL)).token_path.name == "google-token.json"


def test_redacted_never_reveals_a_secret():
    config = load_config(dict(FULL))
    joined = " ".join(config.redacted().values())
    assert "GOCSPX-secret-value" not in joined
    assert "AIza-key" not in joined
    # The webhook URL is the credential; only a short tail may appear.
    assert FULL["GOOGLEAPPS_CHAT_WEBHOOK"] not in joined
    assert "spaces/A" not in joined

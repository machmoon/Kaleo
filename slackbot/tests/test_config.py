"""Configuration: required settings, and never echoing a secret."""

from __future__ import annotations

import pytest

from slackbot.config import Config, ConfigError, load_config

FULL = {
    "SLACK_BOT_TOKEN": "xoxb-abc",
    "SLACK_SIGNING_SECRET": "s3cret",
    "GOOGLE_API_KEY": "AIza-key",
}


def test_a_full_environment_loads():
    config = load_config(dict(FULL))
    assert config.bot_token == "xoxb-abc"
    assert config.port == 3000
    assert config.allowed_channels == frozenset()


def test_every_missing_variable_is_named_at_once():
    """One round of corrections, not three."""
    with pytest.raises(ConfigError) as excinfo:
        load_config({})
    message = str(excinfo.value)
    for name in FULL:
        assert name in message


def test_a_missing_google_key_is_refused_at_startup():
    """Without it every run fails at the first model call, which is a worse
    way to find out."""
    env = dict(FULL)
    del env["GOOGLE_API_KEY"]
    with pytest.raises(ConfigError, match="GOOGLE_API_KEY"):
        load_config(env)


def test_blank_is_treated_as_missing():
    with pytest.raises(ConfigError):
        load_config({**FULL, "SLACK_BOT_TOKEN": "   "})


def test_numbers_are_parsed_and_bad_ones_are_refused():
    config = load_config(
        {**FULL, "SILKSCREEN_SLACK_PORT": "9000", "SILKSCREEN_SLACK_TIME_LIMIT": "45"}
    )
    assert config.port == 9000
    assert config.time_limit_s == 45.0
    with pytest.raises(ConfigError, match="SILKSCREEN_SLACK_PORT"):
        load_config({**FULL, "SILKSCREEN_SLACK_PORT": "eight"})


def test_channel_allowlist_is_parsed_and_enforced():
    config = load_config({**FULL, "SILKSCREEN_SLACK_CHANNELS": "C1, C2 ,"})
    assert config.allowed_channels == frozenset({"C1", "C2"})
    assert config.channel_allowed("C1")
    assert not config.channel_allowed("C9")


def test_an_empty_allowlist_allows_every_channel():
    assert load_config(dict(FULL)).channel_allowed("C-anything")


def test_redacted_never_reveals_a_secret():
    config = Config(bot_token="xoxb-supersecret", signing_secret="hunter2hunter2")
    view = config.redacted()
    joined = " ".join(view.values())
    assert "supersecret" not in joined
    assert "hunter2" not in joined
    assert view["bot_token"] == "<set, 16 chars>"

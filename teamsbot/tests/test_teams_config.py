"""Configuration is validated once, names every gap at once, and leaks nothing.

The bug these tests exist for is not a crash. It is a Teams integration that
starts, looks healthy, and dies on the first real callback -- or one that
quietly falls back to not speaking, which looks exactly like one that is
working.
"""

from __future__ import annotations

import pytest

from teamsbot.config import (
    DEFAULT_GRAPH_BASE,
    DEFAULT_MAX_RUNS_PER_MEETING,
    DEFAULT_PORT,
    DEFAULT_SPEAK_MODE,
    REQUIRED_ENV,
    SPEAK_MODES,
    TEAMS_ENV,
    Config,
    ConfigError,
    load_config,
)

GOOD_ENV = {
    "TEAMS_APP_ID": "11111111-2222-3333-4444-555555555555",
    "TEAMS_APP_SECRET": "s3cr3t-value-nobody-should-ever-see",
    "TEAMS_TENANT_ID": "99999999-8888-7777-6666-555555555555",
}


def env(**overrides: str) -> dict[str, str]:
    merged = dict(GOOD_ENV)
    merged.update(overrides)
    return {key: value for key, value in merged.items() if value is not None}


# -- the happy path -------------------------------------------------------


def test_minimal_env_loads_with_documented_defaults():
    config = load_config(env())
    assert config.app_id == GOOD_ENV["TEAMS_APP_ID"]
    assert config.graph_base == DEFAULT_GRAPH_BASE
    assert config.speak_mode == DEFAULT_SPEAK_MODE
    assert config.max_runs_per_meeting == DEFAULT_MAX_RUNS_PER_MEETING
    assert config.meeting_allowlist == ()
    assert config.bot_endpoint == ""
    assert config.port == DEFAULT_PORT == 3978


def test_every_env_name_is_declared_and_required_is_a_subset():
    # TEAMS_ENV is what the docs, .env.example and the integrations report are
    # checked against, so a variable this module reads but does not declare is
    # a documentation bug waiting to happen.
    assert set(REQUIRED_ENV) <= set(TEAMS_ENV)
    for name in (
        "TEAMS_SPEAK_MODE",
        "TEAMS_MEETINGS",
        "TEAMS_GRAPH_BASE",
        "TEAMS_PORT",
    ):
        assert name in TEAMS_ENV


def test_values_are_stripped_and_allowlist_is_split():
    config = load_config(
        env(
            TEAMS_APP_ID="  padded-id  ",
            TEAMS_MEETINGS=" MEET-A , MEET-B ,, ",
            TEAMS_MAX_RUNS_PER_MEETING=" 5 ",
        )
    )
    assert config.app_id == "padded-id"
    assert config.meeting_allowlist == ("MEET-A", "MEET-B")
    assert config.max_runs_per_meeting == 5


def test_allowlist_gates_meetings_and_empty_means_everything():
    open_config = load_config(env())
    assert open_config.allows("anything-at-all")

    gated = load_config(env(TEAMS_MEETINGS="MEET-A,MEET-B"))
    assert gated.allows("MEET-A")
    assert gated.allows(" MEET-B ")
    assert not gated.allows("MEET-C")


def test_token_url_is_the_tenant_endpoint():
    config = load_config(env())
    assert config.token_url() == (
        f"https://login.microsoftonline.com/{GOOD_ENV['TEAMS_TENANT_ID']}"
        f"/oauth2/v2.0/token"
    )


# -- gaps, all named at once ----------------------------------------------


def test_missing_required_variables_are_all_named_in_one_error():
    with pytest.raises(ConfigError) as excinfo:
        load_config({})
    problems = excinfo.value.errors
    for name in REQUIRED_ENV:
        assert any(name in problem for problem in problems), name
    # And the joined message carries them too, for a caller that only prints.
    for name in REQUIRED_ENV:
        assert name in str(excinfo.value)


def test_one_error_carries_every_kind_of_problem_together():
    with pytest.raises(ConfigError) as excinfo:
        load_config(
            {
                "TEAMS_APP_ID": "",
                "TEAMS_APP_SECRET": "   ",
                "TEAMS_TENANT_ID": "",
                "TEAMS_SPEAK_MODE": "sdk",
                "TEAMS_BOT_ENDPOINT": "",
                "TEAMS_GRAPH_BASE": "http://graph.microsoft.com",
                "TEAMS_MAX_RUNS_PER_MEETING": "0",
                "TEAMS_PORT": "70000",
            }
        )
    joined = str(excinfo.value)
    for name in (
        "TEAMS_APP_ID",
        "TEAMS_APP_SECRET",
        "TEAMS_TENANT_ID",
        "TEAMS_BOT_ENDPOINT",
        "TEAMS_GRAPH_BASE",
        "TEAMS_MAX_RUNS_PER_MEETING",
        "TEAMS_PORT",
    ):
        assert name in joined, name
    # https and version pin are two separate complaints about one value.
    assert sum("TEAMS_GRAPH_BASE" in problem for problem in excinfo.value.errors) == 2


@pytest.mark.parametrize("missing", REQUIRED_ENV)
def test_each_required_variable_alone_is_enough_to_refuse(missing: str):
    partial = env()
    partial.pop(missing)
    with pytest.raises(ConfigError) as excinfo:
        load_config(partial)
    assert excinfo.value.errors == (f"{missing} is not set",)


# -- the https / version pin ----------------------------------------------


@pytest.mark.parametrize(
    "base",
    [
        "http://graph.microsoft.com/v1.0",
        "ftp://graph.microsoft.com/v1.0",
        "graph.microsoft.com/v1.0",
    ],
)
def test_plaintext_graph_base_is_refused(base: str):
    with pytest.raises(ConfigError) as excinfo:
        load_config(env(TEAMS_GRAPH_BASE=base))
    assert any("must be https" in problem for problem in excinfo.value.errors)


@pytest.mark.parametrize(
    "base",
    [
        "https://graph.microsoft.com",
        "https://graph.microsoft.com/",
        "https://graph.microsoft.com/latest",
    ],
)
def test_unpinned_graph_base_is_refused(base: str):
    with pytest.raises(ConfigError) as excinfo:
        load_config(env(TEAMS_GRAPH_BASE=base))
    assert any("version" in problem for problem in excinfo.value.errors)


@pytest.mark.parametrize(
    "base",
    [
        "https://graph.microsoft.com/v1.0",
        "https://graph.microsoft.com/beta",
        "https://graph.microsoft.com/v2",
    ],
)
def test_versioned_https_graph_base_is_accepted(base: str):
    assert load_config(env(TEAMS_GRAPH_BASE=base)).graph_base == base


# -- speak mode -----------------------------------------------------------


@pytest.mark.parametrize("mode", SPEAK_MODES)
def test_each_speak_mode_is_accepted(mode: str):
    extra = {"TEAMS_BOT_ENDPOINT": "https://bot.example.com/api/calls"}
    assert load_config(env(TEAMS_SPEAK_MODE=mode, **extra)).speak_mode == mode


def test_speak_mode_is_case_insensitive():
    assert load_config(env(TEAMS_SPEAK_MODE="OFF")).speak_mode == "off"


def test_unknown_speak_mode_is_an_error_not_a_silent_fallback():
    with pytest.raises(ConfigError) as excinfo:
        load_config(env(TEAMS_SPEAK_MODE="loud"))
    problem = "\n".join(excinfo.value.errors)
    assert "TEAMS_SPEAK_MODE" in problem
    assert "'loud'" in problem
    # The rule that matters: it must not have quietly become a valid mode.
    for mode in SPEAK_MODES:
        with pytest.raises(ConfigError):
            load_config(env(TEAMS_SPEAK_MODE="loud"))
        assert mode != "loud"


def test_sdk_speak_mode_requires_a_bot_endpoint():
    with pytest.raises(ConfigError) as excinfo:
        load_config(env(TEAMS_SPEAK_MODE="sdk"))
    assert any("TEAMS_BOT_ENDPOINT" in p for p in excinfo.value.errors)

    ok = load_config(
        env(TEAMS_SPEAK_MODE="sdk", TEAMS_BOT_ENDPOINT="https://bot.example.com/x")
    )
    assert ok.bot_endpoint == "https://bot.example.com/x"


def test_chat_and_off_do_not_need_a_bot_endpoint():
    for mode in ("chat", "off"):
        assert load_config(env(TEAMS_SPEAK_MODE=mode)).bot_endpoint == ""


def test_plaintext_bot_endpoint_is_refused():
    with pytest.raises(ConfigError) as excinfo:
        load_config(env(TEAMS_BOT_ENDPOINT="http://bot.example.com/api/calls"))
    assert any("must be https" in p for p in excinfo.value.errors)


# -- run cap --------------------------------------------------------------


@pytest.mark.parametrize("raw", ["0", "-1"])
def test_non_positive_run_cap_is_a_typo_not_unlimited(raw: str):
    with pytest.raises(ConfigError) as excinfo:
        load_config(env(TEAMS_MAX_RUNS_PER_MEETING=raw))
    assert any("at least 1" in p for p in excinfo.value.errors)


def test_unparseable_run_cap_is_named_and_does_not_hide_other_problems():
    with pytest.raises(ConfigError) as excinfo:
        load_config(
            {
                "TEAMS_APP_ID": "id",
                "TEAMS_APP_SECRET": "secret",
                "TEAMS_TENANT_ID": "",
                "TEAMS_MAX_RUNS_PER_MEETING": "lots",
            }
        )
    joined = "\n".join(excinfo.value.errors)
    assert "TEAMS_MAX_RUNS_PER_MEETING" in joined
    assert "TEAMS_TENANT_ID" in joined


# -- port -----------------------------------------------------------------


def test_port_is_read_from_the_environment():
    assert load_config(env(TEAMS_PORT="9443")).port == 9443


def test_a_blank_port_falls_back_to_the_default():
    assert load_config(env(TEAMS_PORT="   ")).port == DEFAULT_PORT


@pytest.mark.parametrize("raw", ["0", "-1", "65536", "99999"])
def test_an_unbindable_port_is_refused(raw: str):
    # The callback address registered with Microsoft names a port, so a value
    # that cannot be bound is a deployment that never receives a notification.
    with pytest.raises(ConfigError) as excinfo:
        load_config(env(TEAMS_PORT=raw))
    assert any("TEAMS_PORT" in problem for problem in excinfo.value.errors)
    assert any("between 1 and 65535" in problem for problem in excinfo.value.errors)


def test_an_unparseable_port_is_named_and_does_not_hide_other_problems():
    with pytest.raises(ConfigError) as excinfo:
        load_config(
            {
                "TEAMS_APP_ID": "id",
                "TEAMS_APP_SECRET": "secret",
                "TEAMS_TENANT_ID": "",
                "TEAMS_PORT": "http",
            }
        )
    joined = "\n".join(excinfo.value.errors)
    assert "TEAMS_PORT must be a whole number" in joined
    assert "TEAMS_TENANT_ID" in joined


def test_a_constructed_config_validates_the_port_by_field_name():
    with pytest.raises(ConfigError) as excinfo:
        Config(app_id="a", app_secret="b", tenant_id="c", port=0)
    joined = "\n".join(excinfo.value.errors)
    assert "port must be between 1 and 65535" in joined
    assert "TEAMS_PORT" not in joined


# -- direct construction validates the same way ---------------------------


def test_constructing_a_config_directly_validates_by_field_name():
    with pytest.raises(ConfigError) as excinfo:
        Config(app_id="", app_secret="", tenant_id="")
    joined = "\n".join(excinfo.value.errors)
    assert "app_id is not set" in joined
    assert "app_secret is not set" in joined
    assert "tenant_id is not set" in joined
    # Field names, not env names: this path is a programming error, not a
    # deployment one, and pointing at an env var would misdirect the fix.
    assert "TEAMS_APP_ID" not in joined


def test_constructed_config_rejects_a_bad_speak_mode_too():
    with pytest.raises(ConfigError):
        Config(app_id="a", app_secret="b", tenant_id="c", speak_mode="shout")


# -- redaction ------------------------------------------------------------


def test_redacted_leaks_no_secret_and_no_tail():
    config = load_config(
        env(
            TEAMS_BOT_ENDPOINT="https://bot.example.com/api/calls",
            TEAMS_MEETINGS="MEET-A",
        )
    )
    view = config.redacted()
    blob = " ".join(view.values())

    for secret in (config.app_id, config.app_secret, config.tenant_id):
        assert secret not in blob
        assert secret[-4:] not in blob, "a tail is most of a short secret"
        assert secret[:4] not in blob
    # The endpoint shares a scheme with graph_base, so only the parts that
    # identify it are checked -- the host and the tail.
    assert config.bot_endpoint not in blob
    assert "bot.example.com" not in blob
    assert config.bot_endpoint[-8:] not in blob

    assert view["app_secret"] == f"<set, {len(config.app_secret)} chars>"
    # Non-secret operational settings stay readable -- the point of the view is
    # to prove what was loaded.
    assert view["graph_base"] == DEFAULT_GRAPH_BASE
    assert view["speak_mode"] == "chat"
    assert view["meeting_allowlist"] == "MEET-A"
    assert view["max_runs_per_meeting"] == "2"
    assert view["port"] == str(DEFAULT_PORT)
    assert all(isinstance(value, str) for value in view.values())


def test_redacted_says_unset_rather_than_showing_an_empty_string():
    view = load_config(env()).redacted()
    assert view["bot_endpoint"] == "<unset>"
    assert "any meeting" in view["meeting_allowlist"]

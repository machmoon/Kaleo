"""Configuration is the first thing that can be wrong, so it is tested hardest.

Everything here is offline: no network, no Zoom credentials, no environment
inherited from the machine -- every test passes an explicit ``env`` mapping, so
a developer who happens to have ``ZOOM_CLIENT_ID`` exported cannot make a
failing test pass.
"""

from __future__ import annotations

import pytest

from zoombot.config import (
    DEFAULT_API_BASE,
    DEFAULT_MAX_RUNS_PER_MEETING,
    DEFAULT_SPEAK_MODE,
    REQUIRED_ENV,
    SPEAK_MODES,
    ZOOM_ENV,
    Config,
    ConfigError,
    load_config,
)

CLIENT_ID = "kaleo-client-id-AAAAAAAAAA"
CLIENT_SECRET = "kaleo-client-secret-BBBBBBBBBBBBBBBBBBBB"
ACCOUNT_ID = "kaleo-account-id-CCCCCCCCCC"
WEBHOOK_SECRET = "kaleo-webhook-secret-DDDDDDDDDDDDDDDDDDDD"

SECRET_VALUES = (CLIENT_ID, CLIENT_SECRET, ACCOUNT_ID, WEBHOOK_SECRET)


def env(**overrides: str) -> dict[str, str]:
    """A complete, valid environment, with overrides applied.

    An override of ``""`` removes the variable, which is how a test says
    "this one is missing" without also saying anything about the others.
    """
    base = {
        "ZOOM_CLIENT_ID": CLIENT_ID,
        "ZOOM_CLIENT_SECRET": CLIENT_SECRET,
        "ZOOM_ACCOUNT_ID": ACCOUNT_ID,
        "ZOOM_WEBHOOK_SECRET_TOKEN": WEBHOOK_SECRET,
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v != ""}


def make_config(**overrides) -> Config:
    values = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "account_id": ACCOUNT_ID,
        "webhook_secret": WEBHOOK_SECRET,
    }
    values.update(overrides)
    return Config(**values)


# --- the happy path ---------------------------------------------------------


def test_defaults_are_the_frozen_contract():
    cfg = load_config(env())
    assert cfg.client_id == CLIENT_ID
    assert cfg.webhook_secret == WEBHOOK_SECRET
    assert cfg.api_base == DEFAULT_API_BASE == "https://api.zoom.us/v2"
    assert cfg.speak_mode == DEFAULT_SPEAK_MODE == "chat"
    assert cfg.meeting_allowlist == ()
    assert cfg.max_runs_per_meeting == DEFAULT_MAX_RUNS_PER_MEETING == 2
    assert cfg.rtms_enabled is True


def test_env_roster_covers_every_variable_read():
    # ZOOM_ENV is what the docs and the integrations report are checked
    # against; a variable read but not listed would be invisible to both.
    assert set(REQUIRED_ENV) <= set(ZOOM_ENV)
    for name in (
        "ZOOM_RTMS_ENABLED",
        "ZOOM_SPEAK_MODE",
        "ZOOM_MEETINGS",
        "ZOOM_MAX_RUNS_PER_MEETING",
        "ZOOM_API_BASE",
    ):
        assert name in ZOOM_ENV


def test_whitespace_is_stripped_not_accepted_as_a_value():
    cfg = load_config(env(ZOOM_CLIENT_ID=f"  {CLIENT_ID}  "))
    assert cfg.client_id == CLIENT_ID


def test_a_blank_value_counts_as_missing():
    with pytest.raises(ConfigError) as exc:
        load_config(env(ZOOM_WEBHOOK_SECRET_TOKEN="   "))
    assert exc.value.errors == ("ZOOM_WEBHOOK_SECRET_TOKEN is not set",)


# --- every gap named at once ------------------------------------------------


@pytest.mark.parametrize("missing", REQUIRED_ENV)
def test_each_required_variable_is_named_when_it_alone_is_missing(missing):
    with pytest.raises(ConfigError) as exc:
        load_config(env(**{missing: ""}))
    assert exc.value.errors == (f"{missing} is not set",)
    assert missing in str(exc.value)


def test_an_empty_environment_names_all_four_at_once():
    with pytest.raises(ConfigError) as exc:
        load_config({})
    message = str(exc.value)
    for name in REQUIRED_ENV:
        assert name in message
    assert len(exc.value.errors) == len(REQUIRED_ENV)


@pytest.mark.parametrize(
    "missing",
    [
        ("ZOOM_CLIENT_ID", "ZOOM_CLIENT_SECRET"),
        ("ZOOM_ACCOUNT_ID", "ZOOM_WEBHOOK_SECRET_TOKEN"),
        ("ZOOM_CLIENT_ID", "ZOOM_ACCOUNT_ID", "ZOOM_WEBHOOK_SECRET_TOKEN"),
    ],
)
def test_every_combination_of_gaps_is_reported_together(missing):
    with pytest.raises(ConfigError) as exc:
        load_config(env(**{name: "" for name in missing}))
    assert exc.value.errors == tuple(f"{name} is not set" for name in missing)


def test_a_missing_variable_and_a_bad_value_arrive_in_the_same_error():
    # The failure this guards against: fix the missing secret, restart, and
    # only then learn that the speak mode was a typo. One round, not two.
    with pytest.raises(ConfigError) as exc:
        load_config(
            env(
                ZOOM_CLIENT_SECRET="",
                ZOOM_SPEAK_MODE="loud",
                ZOOM_MAX_RUNS_PER_MEETING="0",
                ZOOM_API_BASE="http://api.zoom.us/v2",
            )
        )
    message = str(exc.value)
    assert "ZOOM_CLIENT_SECRET" in message
    assert "ZOOM_SPEAK_MODE" in message
    assert "ZOOM_MAX_RUNS_PER_MEETING" in message
    assert "ZOOM_API_BASE" in message
    assert len(exc.value.errors) >= 4


def test_error_message_points_at_the_documentation():
    with pytest.raises(ConfigError) as exc:
        load_config({})
    assert "ZOOM_ENV" in str(exc.value)


# --- api_base: https, and pinned to a major version -------------------------


@pytest.mark.parametrize(
    "base",
    [
        "http://api.zoom.us/v2",
        "ftp://api.zoom.us/v2",
        "api.zoom.us/v2",
        "//api.zoom.us/v2",
    ],
)
def test_api_base_must_be_https(base):
    with pytest.raises(ConfigError) as exc:
        load_config(env(ZOOM_API_BASE=base))
    assert any("must be https" in problem for problem in exc.value.errors)


@pytest.mark.parametrize(
    "base",
    [
        "https://api.zoom.us",
        "https://api.zoom.us/",
        "https://api.zoom.us/latest",
        "https://api.zoom.us/v2/meetings",
    ],
)
def test_api_base_must_be_pinned_to_a_major_version(base):
    with pytest.raises(ConfigError) as exc:
        load_config(env(ZOOM_API_BASE=base))
    assert any("major version" in problem for problem in exc.value.errors)


@pytest.mark.parametrize(
    "base", ["https://api.zoom.us/v2", "https://api.zoom.us/v2/", "https://eu01.zoom.us/v3"]
)
def test_a_versioned_https_base_is_accepted(base):
    assert load_config(env(ZOOM_API_BASE=base)).api_base == base


def test_plaintext_and_unpinned_are_two_separate_problems():
    with pytest.raises(ConfigError) as exc:
        load_config(env(ZOOM_API_BASE="http://api.zoom.us/latest"))
    assert len(exc.value.errors) == 2


# --- speak_mode: no silent fallback -----------------------------------------


@pytest.mark.parametrize("mode", SPEAK_MODES)
def test_every_documented_speak_mode_is_accepted(mode):
    assert load_config(env(ZOOM_SPEAK_MODE=mode)).speak_mode == mode


def test_speak_mode_is_case_insensitive():
    assert load_config(env(ZOOM_SPEAK_MODE="SDK")).speak_mode == "sdk"


@pytest.mark.parametrize("mode", ["OFFF", "quiet", "none", "true", "voice", "0"])
def test_an_unknown_speak_mode_is_an_error_not_a_fallback_to_off(mode):
    with pytest.raises(ConfigError) as exc:
        load_config(env(ZOOM_SPEAK_MODE=mode))
    problem = "".join(exc.value.errors)
    assert "ZOOM_SPEAK_MODE" in problem
    # The whole point: it must not have quietly become "off".
    for good in SPEAK_MODES:
        assert good in problem


def test_construction_rejects_a_bad_speak_mode_too():
    # load_config is not the only door into a Config; the dataclass validates
    # for itself, so a caller building one by hand gets the same rules.
    with pytest.raises(ConfigError) as exc:
        make_config(speak_mode="mute")
    assert "speak_mode" in str(exc.value)


# --- counts: each run is paid ----------------------------------------------


@pytest.mark.parametrize("raw,expected", [("1", 1), ("2", 2), ("9", 9)])
def test_max_runs_per_meeting_is_read(raw, expected):
    cfg = load_config(env(ZOOM_MAX_RUNS_PER_MEETING=raw))
    assert cfg.max_runs_per_meeting == expected


@pytest.mark.parametrize("raw", ["0", "-1", "-42"])
def test_a_non_positive_run_cap_is_an_error(raw):
    with pytest.raises(ConfigError) as exc:
        load_config(env(ZOOM_MAX_RUNS_PER_MEETING=raw))
    assert any("at least 1" in problem for problem in exc.value.errors)


@pytest.mark.parametrize("raw", ["two", "1.5", "many", "1,2"])
def test_an_unparseable_run_cap_is_an_error_naming_the_variable(raw):
    with pytest.raises(ConfigError) as exc:
        load_config(env(ZOOM_MAX_RUNS_PER_MEETING=raw))
    assert any("ZOOM_MAX_RUNS_PER_MEETING" in problem for problem in exc.value.errors)


def test_construction_rejects_a_non_positive_run_cap():
    with pytest.raises(ConfigError):
        make_config(max_runs_per_meeting=0)


def test_a_bool_is_not_a_run_cap():
    # True == 1 in Python; accepting it would let a mis-plumbed flag silently
    # become a run budget.
    with pytest.raises(ConfigError):
        make_config(max_runs_per_meeting=True)


# --- rtms toggle ------------------------------------------------------------


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on"])
def test_rtms_can_be_switched_on(raw):
    assert load_config(env(ZOOM_RTMS_ENABLED=raw)).rtms_enabled is True


@pytest.mark.parametrize("raw", ["0", "false", "No", "off"])
def test_rtms_can_be_switched_off(raw):
    assert load_config(env(ZOOM_RTMS_ENABLED=raw)).rtms_enabled is False


def test_an_unparseable_rtms_flag_is_an_error():
    with pytest.raises(ConfigError) as exc:
        load_config(env(ZOOM_RTMS_ENABLED="maybe"))
    assert any("ZOOM_RTMS_ENABLED" in problem for problem in exc.value.errors)


# --- the allowlist ----------------------------------------------------------


def test_allowlist_is_parsed_trimmed_and_ordered_as_written():
    cfg = load_config(env(ZOOM_MEETINGS=" 811 111 1111 , 822222222 ,, "))
    assert cfg.meeting_allowlist == ("811 111 1111", "822222222")


def test_an_empty_allowlist_means_every_meeting_the_credentials_can_see():
    cfg = load_config(env(ZOOM_MEETINGS=""))
    assert cfg.meeting_allowlist == ()
    assert cfg.allows("81111111111")
    assert cfg.allows("anything at all")


def test_allows_admits_only_listed_meetings():
    cfg = load_config(env(ZOOM_MEETINGS="811,822"))
    assert cfg.allows("811")
    assert cfg.allows("822")
    assert not cfg.allows("833")
    assert not cfg.allows("")


def test_allows_tolerates_surrounding_whitespace_on_the_queried_id():
    cfg = load_config(env(ZOOM_MEETINGS="811"))
    assert cfg.allows(" 811 ")


def test_allowlist_is_hashable_and_frozen():
    cfg = load_config(env(ZOOM_MEETINGS="811"))
    assert isinstance(cfg.meeting_allowlist, tuple)
    with pytest.raises(AttributeError):
        cfg.speak_mode = "off"  # frozen dataclass


# --- redacted(): masks and lengths only -------------------------------------


def test_redacted_reports_every_secret_as_a_length():
    shown = load_config(env()).redacted()
    assert shown["client_secret"] == f"<set, {len(CLIENT_SECRET)} chars>"
    assert shown["webhook_secret"] == f"<set, {len(WEBHOOK_SECRET)} chars>"


def test_redacted_contains_no_secret_and_no_usable_tail():
    shown = load_config(env(ZOOM_MEETINGS="811", ZOOM_SPEAK_MODE="sdk")).redacted()
    blob = " ".join(f"{k}={v}" for k, v in shown.items())
    for secret in SECRET_VALUES:
        assert secret not in blob
        # Not even a tail: four characters of a webhook secret token is four
        # characters of a webhook secret token.
        assert secret[-4:] not in blob
        assert secret[:4] not in blob


def test_redacted_still_shows_the_non_secret_settings():
    shown = load_config(
        env(ZOOM_SPEAK_MODE="off", ZOOM_MEETINGS="811,822", ZOOM_RTMS_ENABLED="0")
    ).redacted()
    assert shown["api_base"] == DEFAULT_API_BASE
    assert shown["speak_mode"] == "off"
    assert shown["rtms_enabled"] == "no"
    assert shown["meeting_allowlist"] == "811, 822"
    assert shown["max_runs_per_meeting"] == "2"


def test_redacted_says_any_meeting_when_the_allowlist_is_empty():
    assert "any meeting" in load_config(env()).redacted()["meeting_allowlist"]


def test_redacted_values_are_all_strings():
    for value in load_config(env()).redacted().values():
        assert isinstance(value, str)


# --- the package boundary ---------------------------------------------------


def test_config_is_importable_from_the_package_without_its_siblings():
    # service/integrations.py imports zoombot.config; a sibling module that is
    # missing or mid-edit must not be able to break that.
    import zoombot

    assert zoombot.Config is Config
    assert zoombot.load_config is load_config
    assert zoombot.ConfigError is ConfigError
    assert zoombot.ZOOM_ENV == ZOOM_ENV


def test_unknown_package_attribute_still_raises_attribute_error():
    import zoombot

    with pytest.raises(AttributeError):
        _ = zoombot.definitely_not_a_thing


def test_package_docstring_states_the_unverified_boundary():
    import zoombot

    doc = zoombot.__doc__ or ""
    assert "never been run against a real Zoom" in " ".join(doc.split())
    assert "receive-only" in doc

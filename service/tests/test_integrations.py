"""``GET /integrations`` and the report behind it (``service/integrations.py``).

Every test drives :func:`service.integrations.integrations_report` with an
injected ``environ``, which is what keeps this offline and machine-independent:
nothing here is allowed to depend on whether *this* laptop happens to have
ngspice or KiCad, so the credential-shaped integrations are asserted exactly
and the tool-shaped ones are asserted only against the schema and the four
legal states.

The two properties that matter most have a test each: no secret value ever
appears in the serialised response, and the ``google`` entry agrees with
``GET /deliver/config`` -- the two routes read the same source, so a user can
never be told two different stories about whether Ada can send mail.
"""

import json
import pathlib
import threading
import urllib.error
import urllib.request

import pytest

from service import deliver, integrations
from service.app import Handler, make_server

ENTRY_KEYS = {
    "id",
    "name",
    "kind",
    "state",
    "summary",
    "detail",
    "settings",
    "hints",
    "actions",
    "docs",
    "unverified",
}
SETTING_KEYS = {"key", "required", "set", "shown", "note"}
ACTION_KEYS = {"id", "label", "method", "path"}

#: A fully-configured environment. Every value is deliberately distinctive so
#: :func:`test_no_secret_value_is_ever_echoed` can grep for it.
SECRETS = {
    "SLACK_BOT_TOKEN": "xoxb-1111-BOTTOKENSECRETVALUE",
    "SLACK_SIGNING_SECRET": "SIGNINGSECRETVALUE2222",
    "GOOGLE_API_KEY": "AIzaAPIKEYSECRETVALUE3333",
    "MEET_ACCESS_TOKEN": "ya29.MEETTOKENSECRETVALUE4444",
    "GOOGLEAPPS_CLIENT_SECRET": "CLIENTSECRETVALUE5555",
    "GOOGLEAPPS_CHAT_WEBHOOK": (
        "https://chat.googleapis.com/v1/spaces/AAA/messages"
        "?key=KEYSECRET6666&token=TOKENSECRET7777"
    ),
    "ZOOM_CLIENT_SECRET": "ZOOMCLIENTSECRETVALUE8888",
    "ZOOM_WEBHOOK_SECRET_TOKEN": "ZOOMWEBHOOKSECRETVALUE9999",
    "TEAMS_APP_SECRET": "TEAMSAPPSECRETVALUE0000",
}


def full_env(tmp_path):
    env = dict(SECRETS)
    env.update(
        {
            "GOOGLEAPPS_CLIENT_ID": "client-id.apps.googleusercontent.com",
            "GOOGLEAPPS_TOKEN_PATH": str(tmp_path / "no-token.json"),
            "SILKSCREEN_SLACK_CHANNELS": "C123,C456",
            "SILKSCREEN_SLACK_PORT": "3000",
            "MEET_SPACES": "spaces/abc",
            "MEET_MAX_RUNS_PER_POLL": "2",
            "ZOOM_CLIENT_ID": "zoom-client-id",
            "ZOOM_ACCOUNT_ID": "zoom-account-id",
            "TEAMS_APP_ID": "teams-app-id",
            "TEAMS_TENANT_ID": "teams-tenant-id",
        }
    )
    return env


def by_id(report):
    return {entry["id"]: entry for entry in report["integrations"]}


# ---------------------------------------------------------------- schema


@pytest.mark.parametrize("case", ["empty", "full", "partial"])
def test_every_entry_has_the_frozen_shape(tmp_path, case):
    """The desktop client compiles against this shape; nothing may be absent.

    Checked for all three interesting environments, because a probe that takes
    a different branch when a variable is set is exactly where a key goes
    missing.
    """
    env = {
        "empty": {},
        "full": full_env(tmp_path),
        "partial": {"SLACK_BOT_TOKEN": "xoxb-only-this-one"},
    }[case]
    report = integrations.integrations_report(env)

    assert report["schema_version"] == integrations.SCHEMA_VERSION == 1
    ids = [entry["id"] for entry in report["integrations"]]
    assert tuple(ids) == integrations.INTEGRATION_IDS

    for entry in report["integrations"]:
        assert set(entry) == ENTRY_KEYS, entry["id"]
        assert entry["state"] in ("ready", "partial", "unconfigured", "unavailable")
        assert entry["kind"] in ("delivery", "meeting", "design", "fabrication")
        assert entry["name"] and entry["summary"] and entry["detail"]
        assert entry["docs"]
        assert isinstance(entry["unverified"], bool)
        assert isinstance(entry["hints"], list)
        assert all(isinstance(h, str) and h for h in entry["hints"])
        for setting in entry["settings"]:
            assert set(setting) == SETTING_KEYS, (entry["id"], setting)
            assert isinstance(setting["required"], bool)
            assert isinstance(setting["set"], bool)
            assert isinstance(setting["shown"], str)
            assert isinstance(setting["note"], str)
            # An unset setting has nothing to show; a set one always says so.
            assert bool(setting["shown"]) == setting["set"]
        for action in entry["actions"]:
            assert set(action) == ACTION_KEYS
            assert action["method"] in ("GET", "POST")
            assert action["path"].startswith("/")


def test_the_roster_is_complete_and_frozen():
    """An unbuilt feature still appears -- unbuilt work stays visibly unbuilt."""
    assert integrations.INTEGRATION_IDS == (
        "google",
        "slack",
        "meet",
        "zoom",
        "teams",
        "cad",
        "sourcing",
        "mcp",
        "spice",
        "kicad",
    )
    assert set(integrations._META) == set(integrations.INTEGRATION_IDS)
    assert set(integrations._PROBES) == set(integrations.INTEGRATION_IDS)


#: The repo root, from this file: service/tests/ -> service/ -> root.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_every_docs_link_points_at_a_page_that_exists():
    """A link is a claim. An invented one is the panel telling a small lie.

    The panel renders ``docs`` as "read more", so a path that was right when
    the entry was written and is wrong now is indistinguishable from one that
    was never checked. Anchors are dropped -- this asserts the page exists,
    not that a heading does.
    """
    for ident, (_, _, _, docs, _) in integrations._META.items():
        path, _, _anchor = docs.partition("#")
        assert (REPO_ROOT / path).is_file(), f"{ident} points at a missing {path}"


def test_the_meeting_front_ends_link_to_their_own_pages():
    # Both had docs/integrations-plan.md (the plan, not the manual) until the
    # real pages landed.
    assert integrations._META["zoom"][3] == "docs/zoom.md"
    assert integrations._META["teams"][3] == "docs/teams.md"


def test_the_report_is_json_serialisable(tmp_path):
    """The route hands this straight to ``json.dumps(allow_nan=False)``."""
    json.dumps(integrations.integrations_report(full_env(tmp_path)), allow_nan=False)


# ---------------------------------------------------------------- states


def test_empty_environment_configures_nothing():
    """Zero env, and every credentialed integration says so distinguishably."""
    entries = by_id(integrations.integrations_report({}))
    for ident in ("google", "slack", "meet", "sourcing"):
        assert entries[ident]["state"] in ("unconfigured", "unavailable"), ident
        assert entries[ident]["state"] != "ready"


def test_full_environment_is_ready(tmp_path):
    entries = by_id(integrations.integrations_report(full_env(tmp_path)))
    assert entries["slack"]["state"] == "ready"
    assert entries["slack"]["hints"] == []
    assert "2 channel(s) allowlisted" in entries["slack"]["detail"]
    assert entries["meet"]["state"] == "ready"
    assert "1 space(s) allowlisted" in entries["meet"]["detail"]
    assert entries["sourcing"]["state"] == "ready"
    # A configuration claim, never a claim that anything was sent.
    assert "nothing has been posted" in entries["slack"]["detail"]


def test_partial_names_every_missing_variable_and_the_env_note():
    """One of three set is ``partial``, and the hints are actionable."""
    entry = by_id(
        integrations.integrations_report({"SLACK_BOT_TOKEN": "xoxb-partial"})
    )["slack"]
    assert entry["state"] == "partial"
    hints = " ".join(entry["hints"])
    assert "SLACK_SIGNING_SECRET" in hints
    assert "GOOGLE_API_KEY" in hints
    assert "SLACK_BOT_TOKEN" not in hints  # it is set; do not ask for it again
    assert "the service does not read .env" in hints
    assert deliver._ENV_NOTE in hints


def test_unconfigured_and_partial_stay_distinguishable():
    nothing = by_id(integrations.integrations_report({}))["meet"]
    some = by_id(
        integrations.integrations_report({"MEET_SPACES": "spaces/abc"})
    )["meet"]
    assert nothing["state"] == "unconfigured"
    assert some["state"] == "partial"


def test_zoom_and_teams_never_break_the_route(monkeypatch):
    """An absent (or half-written) meeting package is one entry's state.

    Both packages are being written right now; the import is lazy and every
    failure mode -- missing, syntactically broken, raising on import -- must
    land as ``unavailable`` on that entry alone.
    """
    real_import = __import__

    def exploding(name, *args, **kwargs):
        if name in ("zoombot.config", "teamsbot.config"):
            raise SyntaxError("half-written by another agent")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", exploding)
    entries = by_id(integrations.integrations_report({}))
    for ident in ("zoom", "teams"):
        assert entries[ident]["state"] == "unavailable", ident
        assert "not built here yet" in " ".join(entries[ident]["hints"])
    # ...and the rest of the roster is untouched.
    assert set(entries) == set(integrations.INTEGRATION_IDS)


def test_a_probe_that_raises_becomes_that_entrys_state(monkeypatch):
    """Never a 500: nine good integrations are not lost to one bad probe."""

    def boom(env, explicit):
        raise RuntimeError("probe exploded")

    monkeypatch.setitem(integrations._PROBES, "spice", boom)
    entries = by_id(integrations.integrations_report({}))
    assert entries["spice"]["state"] == "unavailable"
    assert set(entries["spice"]) == ENTRY_KEYS
    assert "probe exploded" in " ".join(entries["spice"]["hints"])
    # ...and every other integration was still reported.
    assert len(entries) == len(integrations.INTEGRATION_IDS)


def test_kicad_env_var_pointing_at_nothing_is_partial_not_missing(tmp_path):
    """``KICAD_CLI`` set but wrong is a misconfiguration, not an absent tool."""
    entry = by_id(
        integrations.integrations_report({"KICAD_CLI": str(tmp_path / "nope")})
    )["kicad"]
    assert entry["state"] == "partial"
    assert "is not a file" in entry["detail"]
    assert deliver._ENV_NOTE in " ".join(entry["hints"])
    assert [s["key"] for s in entry["settings"]] == ["KICAD_CLI"]


def test_optional_tools_report_a_legal_state_whatever_this_machine_has():
    """cad / mcp / spice / kicad depend on the box; only the contract is fixed."""
    entries = by_id(integrations.integrations_report({}))
    for ident in ("cad", "mcp", "spice", "kicad"):
        entry = entries[ident]
        assert entry["state"] in ("ready", "partial", "unconfigured", "unavailable")
        if entry["state"] == "unavailable":
            # An absent extra must name the fix, not just shrug.
            assert entry["hints"], ident


# ---------------------------------------------------------------- secrets


def test_no_secret_value_is_ever_echoed(tmp_path):
    """Not the value, not a tail -- a webhook URL's tail is the token."""
    env = full_env(tmp_path)
    blob = json.dumps(integrations.integrations_report(env))
    for key, value in SECRETS.items():
        assert value not in blob, key
        # Not even a fragment: the last eight characters are enough to matter.
        assert value[-8:] not in blob, key
    assert "<set, " in blob  # the mask is actually used


def test_a_set_secret_shows_only_a_length(tmp_path):
    entry = by_id(integrations.integrations_report(full_env(tmp_path)))["slack"]
    rows = {s["key"]: s for s in entry["settings"]}
    token = rows["SLACK_BOT_TOKEN"]
    assert token["set"] is True
    assert token["shown"] == f"<set, {len(SECRETS['SLACK_BOT_TOKEN'])} chars>"
    # A non-credential setting may show its value; that is the whole point of
    # keeping the two kinds apart.
    assert rows["SILKSCREEN_SLACK_CHANNELS"]["shown"] == "C123,C456"


# ---------------------------------------------------------------- google


def test_google_agrees_with_deliver_config():
    """The two routes read one source, so they cannot tell different stories."""
    report = deliver.config_report()
    entry = by_id(integrations.integrations_report())["google"]

    assert entry["hints"] == [str(h) for h in report["hints"]]
    if not report["available"]:
        assert entry["state"] == "unavailable"
    else:
        assert entry["state"] != "unavailable"
        ready = all(report[k] for k in ("chat", "gmail", "calendar"))
        assert (entry["state"] == "ready") is ready
    # The sign-in action is offered exactly when an OAuth client exists.
    action_ids = [a["id"] for a in entry["actions"]]
    assert ("google_sign_in" in action_ids) is bool(report.get("oauth_client"))


def test_google_uses_the_injected_environment(tmp_path):
    """An injected env must reach ``config_report``, or the entry would report
    the real process environment while its settings rows report the fake one."""
    entry = by_id(integrations.integrations_report(full_env(tmp_path)))["google"]
    assert entry["state"] in ("ready", "partial")
    rows = {s["key"]: s["set"] for s in entry["settings"]}
    assert rows["GOOGLEAPPS_CHAT_WEBHOOK"] is True
    assert rows["GOOGLEAPPS_CLIENT_SECRET"] is True
    # No token was stored, so Gmail/Calendar cannot be claimed as configured.
    assert "sign in" in " ".join(entry["hints"]).lower()


# ---------------------------------------------------------------- the route


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("SILKSCREEN_STEPS_DIR", str(tmp_path / "steps"))
    srv = make_server(port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    Handler.store = None


def get(srv, path):
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{srv.server_port}{path}"
        ) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def test_route_answers_the_full_roster_uncached(server):
    status, headers, body = get(server, "/integrations")
    assert status == 200
    assert headers["Content-Type"] == "application/json"
    assert headers["Cache-Control"] == "no-store"
    payload = json.loads(body)
    assert payload["schema_version"] == 1
    ids = tuple(e["id"] for e in payload["integrations"])
    assert ids == integrations.INTEGRATION_IDS
    for entry in payload["integrations"]:
        assert set(entry) == ENTRY_KEYS



@pytest.mark.parametrize(
    "package, module, names_attr, ident",
    [
        ("zoombot", "zoombot.config", "ZOOM_ENV", "zoom"),
        ("teamsbot", "teamsbot.config", "TEAMS_ENV", "teams"),
    ],
)
def test_settings_match_the_package_env_tuple(package, module, names_attr, ident):
    """The route's copy of the env names is the package's, both ways round.

    ``service/integrations.py`` cannot import the bots at module scope (they
    are optional), so it carries its own list -- which is exactly how a
    variable ends up read by one half and never mentioned by the other. That
    is known issue 3's bug class: ``ZOOM_PORT`` was added to ``ZOOM_ENV``
    after this route was written and went unreported until this test.

    A name in the package but not the report is invisible to the settings
    panel; a name in the report but not the package is dead config someone
    will set and wonder about. Both are failures here.
    """
    config_module = pytest.importorskip(module)
    declared = tuple(getattr(config_module, names_attr))
    entry = next(
        e
        for e in integrations.integrations_report({})["integrations"]
        if e["id"] == ident
    )
    reported = tuple(s["key"] for s in entry["settings"])
    assert set(reported) == set(declared), (
        f"{names_attr} and the {ident} settings rows disagree: "
        f"missing from the report {sorted(set(declared) - set(reported))}, "
        f"unknown to {package} {sorted(set(reported) - set(declared))}"
    )


def test_every_unverified_flag_matches_what_the_docs_claim():
    """The flag is a factual claim, and these are the facts behind it.

    A false marker and a missing one are the same defect: this panel is only
    worth having if its statuses are true. ``google`` is the one that bit --
    it was reported verified because it is the oldest delivery surface, while
    README and CLAUDE.md both say it has never run against live Google APIs,
    and it is the integration that sends real email to real people.
    """
    report = integrations.integrations_report({})
    flags = {row["id"]: row["unverified"] for row in report["integrations"]}

    # Never run against a live account, each stated in the repo's own docs.
    for ident in ("google", "slack", "meet", "zoom", "teams"):
        assert flags[ident] is True, f"{ident} claims to be verified"

    # Local tooling: running it here IS running it live, so there is no
    # unverified state to report.
    for ident in ("cad", "mcp", "spice", "kicad", "sourcing"):
        assert flags[ident] is False, f"{ident} is marked unverified"

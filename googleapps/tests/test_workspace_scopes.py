"""Granular consent: what Google granted, recorded and believed.

Google's consent screen is a checkbox per scope, so "signed in" and "can send
mail" are two different facts. Everything here is about keeping them apart:
the token response's ``scope`` field is the only place the truth exists, so it
is persisted, carried across a refresh that omits it (RFC 6749 §5.1: omitted
means unchanged), and read by every surface that claims a destination works.

The expectations are written against the wire shape Google documents -- a
space-delimited ``scope`` string -- rather than against the record the code
writes, and the membership checks are exact-string rather than substring,
because ``"calendar.events" in scope_string`` is also true for
``calendar.events.readonly`` and that is the bug this file exists to prevent.
"""

from __future__ import annotations

import json
import urllib.parse

import pytest

import googleapps.__main__ as cli
from googleapps import auth
from googleapps.config import Config
from googleapps.tests.fakes import RecordingTransport, config_with_token, valid_token
from googleapps.transport import HttpResponse

NOW = 1_756_600_000.0

GMAIL = auth.GMAIL_SCOPE
CALENDAR = auth.CALENDAR_SCOPE


def oauth_config(tmp_path) -> Config:
    return Config(
        client_id="cid.apps.googleusercontent.com",
        client_secret="csecret",
        token_path=tmp_path / "token.json",
    )


def token_response(**fields) -> dict:
    payload = {
        "access_token": "ya29.first",
        "refresh_token": "1//r",
        "expires_in": 3599,
        "token_type": "Bearer",
    }
    payload.update(fields)
    return payload


def transport_answering(**fields) -> RecordingTransport:
    return RecordingTransport(
        {"oauth2.googleapis.com/token": token_response(**fields)}
    )


def stored(config) -> dict:
    return json.loads(config.token_path.read_text())


# -- the consent URL -------------------------------------------------------


def query_of(url: str) -> dict[str, str]:
    return {
        k: v[0]
        for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()
    }


def test_the_consent_url_forces_the_screen_and_asks_for_offline_access():
    """Without ``prompt=consent`` Google shows an already-authorised user no
    screen at all and returns no refresh token -- which reads as "it never
    asked me anything" and then as an unrefreshable sign-in."""
    query = query_of(auth.build_auth_url("cid", "http://127.0.0.1:9/", "CH", "ST"))
    assert query["prompt"] == "consent"
    assert query["access_type"] == "offline"
    assert query["response_type"] == "code"
    assert query["code_challenge_method"] == "S256"


def test_the_consent_url_asks_for_incremental_authorisation():
    """``build_auth_url`` is callable with a narrower scope set than
    :data:`auth.SCOPES`; without this a second authorisation would mint a
    grant covering only the newer set."""
    assert query_of(auth.build_auth_url("c", "http://127.0.0.1:9/", "C", "S"))[
        "include_granted_scopes"
    ] == "true"


# -- recording what was granted --------------------------------------------


def test_the_exchange_records_exactly_what_google_said_it_granted(tmp_path):
    """The partial grant: Calendar ticked, Gmail unticked. The redirect and
    the token response are both successes; only ``scope`` says otherwise."""
    config = oauth_config(tmp_path)
    auth.exchange_code(
        transport_answering(scope=CALENDAR),
        config,
        code="c", verifier="v", redirect_uri="http://127.0.0.1:1/", now=NOW,
    )
    assert stored(config)["scopes"] == [CALENDAR]
    token = auth.load_token(config.token_path)
    assert auth.scope_state(token, CALENDAR) == "granted"
    assert auth.scope_state(token, GMAIL) == "denied"


def test_a_response_with_no_scope_records_what_was_requested(tmp_path):
    """RFC 6749 §5.1: an omitted ``scope`` means the granted scope is
    identical to the requested one. Recording nothing would turn a full
    grant into "unknown" and lose the check on the very next call."""
    config = oauth_config(tmp_path)
    auth.exchange_code(
        transport_answering(),
        config,
        code="c", verifier="v", redirect_uri="http://127.0.0.1:1/", now=NOW,
        scopes=(GMAIL,),
    )
    assert stored(config)["scopes"] == [GMAIL]


def test_the_granted_set_is_split_on_spaces_not_substring_matched(tmp_path):
    """``"calendar.events" in scope_string`` is true for a token that granted
    only ``calendar.events.readonly``. Membership is exact."""
    config = oauth_config(tmp_path)
    auth.exchange_code(
        transport_answering(scope=f"{CALENDAR}.readonly {GMAIL}"),
        config,
        code="c", verifier="v", redirect_uri="http://127.0.0.1:1/", now=NOW,
    )
    token = auth.load_token(config.token_path)
    assert auth.scope_state(token, CALENDAR) == "denied"
    assert auth.scope_state(token, GMAIL) == "granted"


# -- refresh ---------------------------------------------------------------


def test_a_refresh_that_omits_scope_keeps_the_record(tmp_path):
    """Google usually omits ``scope`` on a refresh. Blanking the record there
    would silently downgrade every known grant to "unknown" the first time a
    token expired."""
    config = config_with_token(
        tmp_path, expires_at=NOW - 10, scopes=[GMAIL, CALENDAR]
    )
    auth.access_token(config, RecordingTransport(), now=NOW)
    assert stored(config)["scopes"] == [GMAIL, CALENDAR]


def test_a_refresh_that_names_scope_is_believed_over_the_record(tmp_path):
    """A scope revoked from the account's permissions page disappears here;
    the record must follow rather than keep claiming the old grant."""
    config = config_with_token(
        tmp_path, expires_at=NOW - 10, scopes=[GMAIL, CALENDAR]
    )
    transport = RecordingTransport(
        {"oauth2.googleapis.com/token": {
            "access_token": "ya29.refreshed", "expires_in": 3599,
            "scope": CALENDAR,
        }}
    )
    auth.access_token(config, transport, now=NOW)
    assert stored(config)["scopes"] == [CALENDAR]


# -- the three states ------------------------------------------------------


def test_an_unrecorded_grant_is_unknown_and_never_a_refusal(tmp_path):
    """A token file written before the record existed says nothing about the
    grant. Refusing on that would break a working sign-in to fix a
    hypothetical one, so "unknown" is a third answer, not a denial."""
    token = valid_token(now=NOW)
    assert auth.granted_scopes(token) is None
    assert auth.scope_state(token, GMAIL) == "unknown"
    assert auth.denied_scopes(token, auth.SCOPES) == []
    auth.require_scopes(token, auth.SCOPES)  # does not raise
    assert auth.destination_state(token, "gmail") == "unknown"
    assert auth.destination_state(None, "gmail") == "unknown"


def test_the_spec_review_rides_the_calendar_scope():
    assert auth.DESTINATION_SCOPES["spec_review"] == CALENDAR


def test_a_declined_scope_names_the_checkbox_and_the_fix():
    token = valid_token(now=NOW, scopes=[CALENDAR])
    with pytest.raises(auth.AuthError) as excinfo:
        auth.require_scopes(token, (GMAIL, CALENDAR))
    message = str(excinfo.value)
    assert GMAIL in message
    assert auth.SCOPE_LABELS[GMAIL] in message
    assert "python -m googleapps auth" in message
    # The scope that *was* granted is not blamed.
    assert CALENDAR not in message.replace(GMAIL, "")


def test_access_token_refuses_a_scope_the_record_says_was_declined(tmp_path):
    config = config_with_token(tmp_path, expires_at=NOW + 3000, scopes=[CALENDAR])
    transport = RecordingTransport()
    # The Calendar half of the same token still works.
    assert auth.access_token(
        config, transport, now=NOW, require=(CALENDAR,)
    ) == "ya29.live-token"
    with pytest.raises(auth.AuthError, match="gmail.send"):
        auth.access_token(config, transport, now=NOW, require=(GMAIL,))


def test_token_scopes_reads_the_file_and_survives_a_missing_one(tmp_path):
    config = config_with_token(tmp_path, scopes=[GMAIL])
    assert auth.token_scopes(config.token_path) == (GMAIL,)
    assert auth.token_scopes(tmp_path / "nope.json") is None


# -- the CLI ---------------------------------------------------------------


def test_run_email_refuses_before_the_pipeline_when_gmail_was_declined(
    tmp_path, monkeypatch, capsys
):
    """The repo's rule: everything checkable is checked before a model call.
    A declined checkbox discovered at the Gmail call has already paid for a
    board."""
    config = config_with_token(tmp_path, scopes=[CALENDAR])
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(
        cli, "run_pipeline",
        lambda *a, **k: pytest.fail("a paid run must not start without the scope"),
    )
    code = cli.main(
        ["run", "an LDO", "--email", "a@example.com"],
        transport=RecordingTransport(),
    )
    assert code == 2
    err = capsys.readouterr().err
    assert GMAIL in err
    assert "python -m googleapps auth" in err


def test_run_schedule_refuses_before_the_pipeline_when_calendar_was_declined(
    tmp_path, monkeypatch, capsys
):
    config = config_with_token(tmp_path, scopes=[GMAIL])
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(
        cli, "run_pipeline",
        lambda *a, **k: pytest.fail("a paid run must not start without the scope"),
    )
    code = cli.main(
        ["run", "an LDO", "--schedule", "--attendee", "a@example.com"],
        transport=RecordingTransport(),
    )
    assert code == 2
    assert CALENDAR in capsys.readouterr().err


def test_run_email_is_not_refused_on_an_unrecorded_grant(
    tmp_path, monkeypatch, capsys
):
    """The compatibility half of the same rule, and the reason "unknown" is
    not folded into "denied": a token stored before the record existed keeps
    working."""
    config = config_with_token(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    started: list[bool] = []

    def run(*args, **kwargs):
        started.append(True)
        raise RuntimeError("far enough: the pre-flight let the run start")

    monkeypatch.setattr(cli, "run_pipeline", run)
    cli.main(
        ["run", "an LDO", "--email", "a@example.com"],
        transport=RecordingTransport(),
    )
    assert started == [True]


def test_check_says_which_destinations_the_sign_in_can_actually_reach(
    tmp_path, monkeypatch, capsys
):
    config = config_with_token(tmp_path, scopes=[CALENDAR])
    monkeypatch.setattr(cli, "load_config", lambda: config)
    assert cli.main(["check"], transport=RecordingTransport()) == 0
    out = capsys.readouterr().out
    assert "NOT GRANTED" in out
    assert GMAIL in out
    # Still local: the token itself is never printed.
    assert "ya29.live-token" not in out


def test_check_says_unrecorded_rather_than_guessing(tmp_path, monkeypatch, capsys):
    config = config_with_token(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    cli.main(["check"], transport=RecordingTransport())
    out = capsys.readouterr().out
    assert "not recorded" in out
    assert "NOT GRANTED" not in out


def test_auth_reports_the_partial_grant_it_just_received(tmp_path, monkeypatch,
                                                         capsys):
    """A sign-in that returns half of what was asked for is a success at the
    protocol level; the person still has to be told which half."""
    config = oauth_config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: config)

    def authorize(build_url, state):
        build_url("http://127.0.0.1:5")
        return f"/?state={urllib.parse.quote(state)}&code=c"

    real_flow = auth.run_auth_flow
    monkeypatch.setattr(
        cli.auth, "run_auth_flow",
        lambda cfg, tr: real_flow(cfg, tr, authorize=authorize, now=NOW),
    )
    code = cli.main(["auth"], transport=transport_answering(scope=CALENDAR))
    assert code == 0
    out = capsys.readouterr().out
    assert "Signed in" in out
    assert "NOT GRANTED" in out
    assert GMAIL in out


def test_a_declined_consent_is_still_a_failure_not_a_partial_grant(tmp_path):
    """Unticking every box is ``error=access_denied``, which the redirect
    parser already refuses; nothing is stored."""
    config = oauth_config(tmp_path)
    with pytest.raises(auth.AuthError, match="access_denied"):
        auth.run_auth_flow(
            config,
            RecordingTransport(),
            authorize=lambda build_url, state: (
                build_url("http://127.0.0.1:5")
                and f"/?state={urllib.parse.quote(state)}&error=access_denied"
            ),
            now=NOW,
        )
    assert not config.token_path.exists()


def test_a_token_error_is_still_a_token_error(tmp_path):
    """The scope record must not swallow the failure paths above it."""
    transport = RecordingTransport(
        {"oauth2.googleapis.com/token": HttpResponse(
            400, b'{"error": "invalid_grant"}')}
    )
    with pytest.raises(auth.AuthError):
        auth.exchange_code(
            transport, oauth_config(tmp_path), code="c", verifier="v",
            redirect_uri="http://127.0.0.1:1/", now=NOW,
        )

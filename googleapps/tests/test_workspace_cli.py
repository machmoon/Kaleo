"""The subcommands: check stays local, run gates the review event on blockers."""

from __future__ import annotations

import base64
import email
import email.policy
import json
import pathlib

import pytest
from silkscreen.agents.review import Severity

import googleapps.__main__ as cli
from googleapps.config import Config
from googleapps.runner import RunOutcome, StageLog, email_body
from googleapps.tests.fakes import (
    WEBHOOK,
    RecordingTransport,
    config_with_token,
    fake_result,
    fake_route,
    finding,
)
from googleapps.transport import HttpResponse


@pytest.fixture
def configured(tmp_path, monkeypatch):
    """A fully-configured environment with a valid token file on disk."""
    config = config_with_token(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    return config


def outcome(tmp_path, **kwargs):
    board = tmp_path / "out" / "board.kicad_pcb"
    board.parent.mkdir(parents=True, exist_ok=True)
    board.write_bytes(b"(kicad_pcb (version 20240108))\n")
    kwargs.setdefault("board_path", board)
    return RunOutcome(
        result=fake_result(**kwargs),
        stage_lines=["place: done in 1.2 s"],
        duration_s=3.4,
    )


def wire_run(monkeypatch, tmp_path, **kwargs):
    run = outcome(tmp_path, **kwargs)
    monkeypatch.setattr(cli, "run_pipeline", lambda *a, **k: run)
    return run


# -- check -----------------------------------------------------------------


def test_check_is_purely_local_and_never_prints_a_secret(configured, capsys):
    transport = RecordingTransport()
    assert cli.main(["check"], transport=transport) == 0
    assert transport.requests == []
    out = capsys.readouterr().out
    assert "valid" in out
    assert "client-secret-value" not in out
    assert "AIza-key" not in out
    assert WEBHOOK not in out
    assert "ya29.live-token" not in out


def test_check_reports_a_valid_webhook_shape(configured, capsys):
    cli.main(["check"], transport=RecordingTransport())
    assert "webhook shape   valid" in capsys.readouterr().out


def test_check_reports_an_invalid_webhook_shape_without_repeating_it(
    tmp_path, monkeypatch, capsys
):
    url = "https://evil.example/v1/spaces/SECRET/messages?token=hunter2"
    monkeypatch.setattr(
        cli, "load_config",
        lambda: Config(chat_webhook=url, token_path=tmp_path / "none.json"),
    )
    assert cli.main(["check"], transport=RecordingTransport()) == 0
    out = capsys.readouterr().out
    assert "INVALID" in out
    assert "SECRET" not in out and "hunter2" not in out


def test_check_reports_a_missing_token_with_the_fix(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "load_config", lambda: Config(token_path=tmp_path / "none.json")
    )
    assert cli.main(["check"], transport=RecordingTransport()) == 0
    out = capsys.readouterr().out
    assert "missing" in out
    assert "python -m googleapps auth" in out


# -- run: config gates before model spend ----------------------------------


def test_run_without_an_api_key_refuses_before_the_pipeline(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda: Config())
    monkeypatch.setattr(
        cli, "run_pipeline",
        lambda *a, **k: pytest.fail("the pipeline must not run unconfigured"),
    )
    assert cli.main(["run", "an LDO"], transport=RecordingTransport()) == 2
    assert "GOOGLE_API_KEY" in capsys.readouterr().err


def test_run_with_email_but_no_token_refuses_before_the_pipeline(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(
        cli, "load_config",
        lambda: Config(google_api_key="k", token_path=tmp_path / "none.json"),
    )
    monkeypatch.setattr(
        cli, "run_pipeline",
        lambda *a, **k: pytest.fail("a paid run must not start without a token"),
    )
    code = cli.main(
        ["run", "an LDO", "--email", "a@example.com"], transport=RecordingTransport()
    )
    assert code == 2
    assert "python -m googleapps auth" in capsys.readouterr().err


def never_run(monkeypatch, why: str):
    monkeypatch.setattr(cli, "run_pipeline", lambda *a, **k: pytest.fail(why))


def test_a_revoked_refresh_refuses_before_the_pipeline(
    tmp_path, monkeypatch, capsys
):
    """The token file exists, so the old check passed; the refresh Google
    would answer is invalid_grant. That must surface before model spend."""
    config = config_with_token(tmp_path, expires_at=1.0)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    never_run(monkeypatch, "a paid run must not start on a revoked token")
    revoked = HttpResponse(400, b'{"error": "invalid_grant"}')
    transport = RecordingTransport({"oauth2.googleapis.com/token": revoked})
    code = cli.main(
        ["run", "an LDO", "--email", "a@example.com"], transport=transport
    )
    assert code == 2
    assert "python -m googleapps auth" in capsys.readouterr().err


def test_a_valid_but_unrefreshable_token_refuses_before_the_pipeline(
    tmp_path, monkeypatch, capsys
):
    """Valid now is not enough: the run can outlast the token, and the
    refresh it will then need has no OAuth client / no refresh token."""
    never_run(monkeypatch, "a paid run must not start on an unrefreshable token")
    # No refresh token at all.
    config = config_with_token(tmp_path, refresh_token="")
    monkeypatch.setattr(cli, "load_config", lambda: config)
    code = cli.main(
        ["run", "an LDO", "--email", "a@example.com"], transport=RecordingTransport()
    )
    assert code == 2
    assert "python -m googleapps auth" in capsys.readouterr().err
    # A refresh token, but no OAuth client to use it with.
    good = config_with_token(tmp_path)
    monkeypatch.setattr(
        cli, "load_config",
        lambda: Config(google_api_key="k", token_path=good.token_path),
    )
    code = cli.main(
        ["run", "an LDO", "--email", "a@example.com"], transport=RecordingTransport()
    )
    assert code == 2
    assert "GOOGLEAPPS_CLIENT_ID" in capsys.readouterr().err


def test_an_attendee_without_schedule_is_refused_rather_than_ignored(
    configured, monkeypatch, capsys
):
    never_run(monkeypatch, "flag validation happens before the run")
    code = cli.main(
        ["run", "an LDO", "--attendee", "lead@example.com"],
        transport=RecordingTransport(),
    )
    assert code == 2
    assert "--schedule" in capsys.readouterr().err


def test_a_bad_model_name_is_a_configuration_error(configured, monkeypatch, capsys):
    def boom(*a, **k):
        raise cli.ModelError("unknown model 'gemini-nope'")

    monkeypatch.setattr(cli, "run_pipeline", boom)
    code = cli.main(["run", "an LDO", "--model", "gemini-nope"],
                    transport=RecordingTransport())
    assert code == 2
    assert "gemini-nope" in capsys.readouterr().err


def test_run_with_an_expired_token_refreshes_it_before_the_pipeline(
    tmp_path, monkeypatch
):
    config = config_with_token(tmp_path, expires_at=1.0)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    wire_run(monkeypatch, tmp_path)
    transport = RecordingTransport()
    code = cli.main(
        ["run", "an LDO", "-o", str(tmp_path / "out" / "board.kicad_pcb"),
         "--email", "a@example.com"],
        transport=transport,
    )
    assert code == 0
    assert "oauth2.googleapis.com/token" in transport.requests[0].url
    assert transport.called("gmail.googleapis.com")
    # The refreshed token, not the expired one, went out on the send.
    send = next(r for r in transport.requests if "gmail" in r.url)
    assert send.headers["Authorization"] == "Bearer ya29.refreshed"


def test_a_bad_email_address_refuses_before_the_pipeline(
    configured, monkeypatch, capsys
):
    never_run(monkeypatch, "address validation happens before the run")
    code = cli.main(
        ["run", "an LDO", "--email", "not an address"], transport=RecordingTransport()
    )
    assert code == 2
    assert "--email" in capsys.readouterr().err


def test_a_bad_attendee_refuses_before_the_pipeline(configured, monkeypatch, capsys):
    never_run(monkeypatch, "address validation happens before the run")
    code = cli.main(
        ["run", "an LDO", "--schedule", "--attendee", "lead@example.com, x@y.z"],
        transport=RecordingTransport(),
    )
    assert code == 2
    assert "--attendee" in capsys.readouterr().err


def test_a_malformed_datasheet_flag_refuses_before_the_pipeline(
    configured, monkeypatch, capsys
):
    never_run(monkeypatch, "flag validation happens before the run")
    code = cli.main(["run", "an LDO", "-d", "AMS1117"], transport=RecordingTransport())
    assert code == 2
    assert "PART=URL" in capsys.readouterr().err


def test_pipeline_flags_reach_the_runner_under_the_cli_s_names(
    configured, monkeypatch, tmp_path
):
    seen: dict = {}

    def capture(config, intent, output, **kwargs):
        seen.update(kwargs)
        return outcome(tmp_path)

    monkeypatch.setattr(cli, "run_pipeline", capture)
    code = cli.main(
        ["run", "an LDO", "-o", str(tmp_path / "out" / "board.kicad_pcb"),
         "-d", "AMS1117-3.3=https://example.com/ams1117.pdf",
         "--no-route", "--no-review", "--repairs", "5",
         "--model", "gemini-test", "--time-limit", "3"],
        transport=RecordingTransport(),
    )
    assert code == 0
    assert seen["datasheets"] == {"AMS1117-3.3": "https://example.com/ams1117.pdf"}
    assert seen["route"] is False
    assert seen["review"] is False
    assert seen["max_repairs"] == 5
    assert seen["model_name"] == "gemini-test"
    assert seen["time_limit_s"] == 3.0


def test_the_email_subject_is_the_verdict_not_the_whole_summary(
    configured, monkeypatch, tmp_path
):
    wire_run(monkeypatch, tmp_path, findings=[finding()])
    transport = RecordingTransport()
    cli.main(
        ["run", "an LDO", "-o", str(tmp_path / "out" / "board.kicad_pcb"),
         "--email", "a@example.com"],
        transport=transport,
    )
    body = json.loads(next(r for r in transport.requests if "gmail" in r.url).body)
    raw = base64.urlsafe_b64decode(body["raw"].encode("ascii"))
    message = email.message_from_bytes(raw, policy=email.policy.default)
    assert message["Subject"] == "silkscreen: board — needs review — 1 blocker(s)"


def test_schedule_needs_an_attendee(configured, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        cli, "run_pipeline",
        lambda *a, **k: pytest.fail("flag validation happens before the run"),
    )
    code = cli.main(
        ["run", "an LDO", "-o", str(tmp_path / "b.kicad_pcb"), "--schedule"],
        transport=RecordingTransport(),
    )
    assert code == 2
    assert "--attendee" in capsys.readouterr().err


# -- run: delivery ---------------------------------------------------------


def test_a_plain_run_delivers_nothing(configured, monkeypatch, tmp_path, capsys):
    wire_run(monkeypatch, tmp_path)
    transport = RecordingTransport()
    assert cli.main(
        ["run", "an LDO", "-o", str(tmp_path / "out" / "board.kicad_pcb")],
        transport=transport,
    ) == 0
    assert transport.requests == []
    assert "board" in capsys.readouterr().out


def test_chat_and_email_each_reach_their_endpoint(
    configured, monkeypatch, tmp_path
):
    wire_run(monkeypatch, tmp_path)
    transport = RecordingTransport()
    code = cli.main(
        [
            "run", "an LDO",
            "-o", str(tmp_path / "out" / "board.kicad_pcb"),
            "--chat",
            "--email", "team@example.com",
        ],
        transport=transport,
    )
    assert code == 0
    assert transport.called("chat.googleapis.com/v1/spaces")
    assert transport.called("gmail.googleapis.com")


def test_schedule_creates_no_event_when_the_review_is_clean(
    configured, monkeypatch, tmp_path, capsys
):
    wire_run(monkeypatch, tmp_path)  # no findings at all
    transport = RecordingTransport()
    code = cli.main(
        [
            "run", "an LDO",
            "-o", str(tmp_path / "out" / "board.kicad_pcb"),
            "--schedule", "--attendee", "lead@example.com",
        ],
        transport=transport,
    )
    assert code == 0
    assert not transport.called("calendar")
    assert "no blockers" in capsys.readouterr().out


def test_schedule_creates_the_event_only_because_of_blockers(
    configured, monkeypatch, tmp_path, capsys
):
    wire_run(
        monkeypatch, tmp_path,
        findings=[finding(Severity.BLOCKER, "VIN has no bulk capacitor"),
                  finding(Severity.NOTE, "informational")],
    )
    transport = RecordingTransport()
    code = cli.main(
        [
            "run", "an LDO",
            "-o", str(tmp_path / "out" / "board.kicad_pcb"),
            "--schedule", "--attendee", "lead@example.com",
        ],
        transport=transport,
    )
    assert code == 0
    assert transport.called("calendars/primary/events?conferenceDataVersion=1")
    body = json.loads(
        [r for r in transport.requests if "calendar" in r.url][0].body
    )
    assert body["attendees"] == [{"email": "lead@example.com"}]
    assert "1 blocker(s)" in capsys.readouterr().out


def test_a_skipped_review_is_reported_as_skipped_not_clean(
    configured, monkeypatch, tmp_path, capsys
):
    """--no-review means "no blockers" is not a fact we hold; say so."""
    wire_run(monkeypatch, tmp_path)
    transport = RecordingTransport()
    code = cli.main(
        [
            "run", "an LDO",
            "-o", str(tmp_path / "out" / "board.kicad_pcb"),
            "--no-review", "--schedule", "--attendee", "lead@example.com",
        ],
        transport=transport,
    )
    assert code == 0
    assert not transport.called("calendar")
    out = capsys.readouterr().out
    assert "review was skipped" in out
    assert "no blockers" not in out


def test_a_failed_review_is_reported_as_failed_not_clean(
    configured, monkeypatch, tmp_path, capsys
):
    """The critic answered something unreadable, so it holds no more of a fact
    than --no-review does. Skipping the invite while printing "review found no
    blockers" is the same silent zero, reached a different way.
    """
    from silkscreen.agents.review import ReviewReport, ReviewStatus

    wire_run(
        monkeypatch, tmp_path,
        review=ReviewReport(status=ReviewStatus.FAILED, detail="not JSON"),
    )
    transport = RecordingTransport()
    code = cli.main(
        [
            "run", "an LDO",
            "-o", str(tmp_path / "out" / "board.kicad_pcb"),
            "--schedule", "--attendee", "lead@example.com",
        ],
        transport=transport,
    )
    assert code == 0
    assert not transport.called("calendar")
    out = capsys.readouterr().out
    assert "review failed to produce a readable answer" in out
    assert "nothing is known about this board" in out
    assert "review found no blockers" not in out


def test_the_email_body_says_the_review_produced_no_verdict():
    """A body with no "Review blockers:" section reads as a board with no
    blockers, and the reader cannot see that the critic never answered."""
    import tempfile

    from silkscreen.agents.review import ReviewReport, ReviewStatus

    with tempfile.TemporaryDirectory() as tmp:
        run = outcome(
            pathlib.Path(tmp),
            review=ReviewReport(status=ReviewStatus.FAILED, detail="not JSON"),
        )
    body = email_body(run)
    assert "Review: the review failed to produce a readable answer" in body
    assert "has not been argued against" in body


def test_a_marginal_only_review_does_not_schedule(
    configured, monkeypatch, tmp_path
):
    wire_run(monkeypatch, tmp_path, findings=[finding(Severity.MARGINAL, "tight")])
    transport = RecordingTransport()
    cli.main(
        [
            "run", "an LDO",
            "-o", str(tmp_path / "out" / "board.kicad_pcb"),
            "--schedule", "--attendee", "lead@example.com",
        ],
        transport=transport,
    )
    assert not transport.called("calendar")


# -- --spec-review ---------------------------------------------------------


def test_spec_review_needs_an_attendee(configured, monkeypatch, tmp_path, capsys):
    wire_run(monkeypatch, tmp_path)
    transport = RecordingTransport()
    code = cli.main(
        ["run", "an LDO", "-o", str(tmp_path / "b.kicad_pcb"), "--spec-review"],
        transport=transport,
    )
    assert code == 2
    assert "--spec-review needs at least one --attendee" in capsys.readouterr().err
    assert transport.requests == []


def test_spec_review_books_the_agenda_with_a_meet_link(
    configured, monkeypatch, tmp_path, capsys
):
    wire_run(
        monkeypatch, tmp_path,
        findings=[finding(Severity.BLOCKER, "VIN has no bulk capacitor")],
        route=fake_route(unrouted={"VOUT": "no path at 0.25 mm clearance"}),
    )
    transport = RecordingTransport(
        {"calendars/primary/events": {
            "htmlLink": "https://calendar.google.com/event?eid=z",
            "hangoutLink": "https://meet.google.com/aaa-bbbb-ccc"}}
    )
    code = cli.main(
        [
            "run", "an LDO",
            "-o", str(tmp_path / "out" / "board.kicad_pcb"),
            "--spec-review", "--attendee", "lead@example.com",
        ],
        transport=transport,
    )
    assert code == 0
    body = json.loads([r for r in transport.requests if "calendar" in r.url][0].body)
    assert body["summary"].startswith("Spec review: board")
    assert "VIN has no bulk capacitor" in body["description"]
    # The ratsnest is named; nothing here may read as "board ready".
    assert "VOUT: no path at 0.25 mm clearance" in body["description"]
    out = capsys.readouterr().out
    assert "-minute spec review" in out
    assert "meet: https://meet.google.com/aaa-bbbb-ccc" in out


def test_spec_review_books_nothing_when_nothing_is_blocking(
    configured, monkeypatch, tmp_path, capsys
):
    wire_run(monkeypatch, tmp_path, findings=[finding(Severity.NOTE, "cosmetic")])
    transport = RecordingTransport()
    code = cli.main(
        [
            "run", "an LDO",
            "-o", str(tmp_path / "out" / "board.kicad_pcb"),
            "--spec-review", "--attendee", "lead@example.com",
        ],
        transport=transport,
    )
    assert code == 0
    assert not transport.called("calendar")
    assert "no spec review was booked" in capsys.readouterr().out


def test_spec_review_says_the_review_was_skipped_rather_than_guessing(
    configured, monkeypatch, tmp_path, capsys
):
    wire_run(monkeypatch, tmp_path)
    transport = RecordingTransport()
    code = cli.main(
        [
            "run", "an LDO",
            "-o", str(tmp_path / "out" / "board.kicad_pcb"),
            "--no-review", "--spec-review", "--attendee", "lead@example.com",
        ],
        transport=transport,
    )
    assert code == 0
    assert not transport.called("calendar")
    assert "review was skipped" in capsys.readouterr().out


def test_spec_review_says_the_review_failed_rather_than_booking_nothing(
    configured, monkeypatch, tmp_path, capsys
):
    """The --schedule rule, on the --spec-review path: a critic that answered
    nothing readable leaves an empty agenda that is not evidence of a clean
    board, so the skip must say "failed", never "nothing is blocking"."""
    from silkscreen.agents.review import ReviewReport, ReviewStatus

    wire_run(
        monkeypatch, tmp_path,
        review=ReviewReport(status=ReviewStatus.FAILED, detail="not JSON"),
    )
    transport = RecordingTransport()
    code = cli.main(
        [
            "run", "an LDO",
            "-o", str(tmp_path / "out" / "board.kicad_pcb"),
            "--spec-review", "--attendee", "lead@example.com",
        ],
        transport=transport,
    )
    assert code == 0
    assert not transport.called("calendar")
    out = capsys.readouterr().out
    assert "review failed to produce a readable answer" in out
    assert "nothing on the agenda is blocking" not in out


# -- the stage log and the email body --------------------------------------


def test_the_stage_log_only_ticks_stages_whose_events_arrived():
    log = StageLog()
    log.on_event({"event": "stage.start", "stage": "place", "t_s": 1.0})
    log.on_event({"event": "stage.start", "stage": "route", "t_s": 4.5})
    log.on_event({"event": "stage.done", "stage": "place", "t_s": 4.5})
    assert log.lines() == ["place: done in 3.5 s"]  # route never finished


def test_the_email_body_names_every_unrouted_net(tmp_path):
    run = outcome(
        tmp_path,
        route=fake_route(unrouted={"SWD_CLK": "no path at 0.25 mm clearance"}),
        findings=[finding()],
    )
    body = email_body(run)
    assert "SWD_CLK: no path at 0.25 mm clearance" in body
    assert "VIN has no bulk capacitor" in body
    assert "place: done in 1.2 s" in body

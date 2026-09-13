"""The Google Workspace delivery routes (``service/deliver.py``).

Offline: every Google call goes to ``googleapps``' recording transport through
the module seam, and the sessions are built by hand from the package's own
fakes so a test can say exactly which nets are unrouted and which findings
block -- the honesty rules are about text, and hand-built results are the only
way to assert exact text. One test runs the real scripted pipeline through
``/steps`` first, so the adapter is also exercised against a genuine session.
"""

import base64
import email
import email.policy
import json
import threading
import urllib.error
import urllib.request

import pytest
from silkscreen.agents.review import ReviewReport, ReviewStatus, Severity

from googleapps.tests.fakes import (
    WEBHOOK,
    FakeSpec,
    RecordingTransport,
    fake_board,
    fake_route,
    finding,
    save_token,
    valid_token,
)
from service import deliver, steps
from service.app import Handler, make_server
from service.cache import MemoryFactStore
from service.tests.test_app import scripted, url


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("SILKSCREEN_STEPS_DIR", str(tmp_path / "steps"))
    monkeypatch.setenv("SILKSCREEN_KICAD_LIVE_PYTHON", str(tmp_path / "missing-python"))
    for name in (
        "GOOGLEAPPS_CHAT_WEBHOOK",
        "GOOGLEAPPS_CLIENT_ID",
        "GOOGLEAPPS_CLIENT_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GOOGLEAPPS_TOKEN_PATH", str(tmp_path / "no-token.json"))
    steps.reset_sessions()
    Handler.model_factory = staticmethod(scripted)
    Handler.store = MemoryFactStore()
    srv = make_server(port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    Handler.store = None
    deliver.transport_factory = None
    steps.reset_sessions()


@pytest.fixture
def configured(tmp_path, monkeypatch):
    """Everything set: webhook, OAuth client, a valid stored token, a fake wire."""
    token_path = tmp_path / "google-token.json"
    save_token(token_path, valid_token())
    monkeypatch.setenv("GOOGLEAPPS_CHAT_WEBHOOK", WEBHOOK)
    monkeypatch.setenv("GOOGLEAPPS_CLIENT_ID", "client-id.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLEAPPS_CLIENT_SECRET", "client-secret-value")
    monkeypatch.setenv("GOOGLEAPPS_TOKEN_PATH", str(token_path))
    transport = RecordingTransport(
        {
            "calendar/v3": {
                "htmlLink": "https://calendar.google.com/event?eid=abc",
                "conferenceData": {
                    "entryPoints": [
                        {"entryPointType": "video", "uri": "https://meet.google.com/abc-defg-hij"}
                    ]
                },
            }
        }
    )
    deliver.transport_factory = lambda: transport
    return transport


def post(srv, path, payload):
    req = urllib.request.Request(
        url(srv, path),
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def get(srv, path):
    try:
        with urllib.request.urlopen(url(srv, path)) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


BOARD_BYTES = b"(kicad_pcb (version 20240108) (generator silkscreen))\n"


def routed_session(
    tmp_path,
    *,
    stage="routed",
    findings=None,
    reviewed=True,
    unrouted=None,
    review=None,
    spec_review=None,
):
    """A session the way ``steps.py`` leaves one after the route step."""
    directory = tmp_path / "steps" / "sess1"
    directory.mkdir(parents=True, exist_ok=True)
    board = directory / "a-3-3v-regulator.kicad_pcb"
    board.write_bytes(BOARD_BYTES)
    done = {"place", "route"} | ({"review"} if reviewed else set())
    session = steps.Session(
        id="sess1",
        intent="a 3.3V regulator",
        stem="a-3-3v-regulator",
        directory=directory,
        kicad_live=False,
        time_limit_s=5.0,
        stage=stage,
        done=done,
        spec=FakeSpec(),
        board=fake_board(),
        route=fake_route(unrouted=unrouted or {}),
        findings=list(findings or []),
        review=review,
        spec_review=spec_review,
        files={"board": str(board)} if stage == "routed" else {},
    )
    steps._register(session)
    return session


def failed_review():
    """A critic that was asked and answered something unreadable."""
    return ReviewReport(
        status=ReviewStatus.FAILED, detail="the critic's answer was not JSON"
    )


# ---------------------------------------------------------------- config


def test_config_with_nothing_configured_names_every_fix(server):
    status, body = get(server, "/deliver/config")
    assert status == 200
    assert body["available"] is True
    assert body["chat"] is False
    assert body["gmail"] is False and body["calendar"] is False
    assert body["oauth_client"] is False
    assert body["signed_in"] is False
    assert body["token"] == "missing"
    hints = "\n".join(body["hints"])
    assert "GOOGLEAPPS_CHAT_WEBHOOK" in hints
    assert "GOOGLEAPPS_CLIENT_ID" in hints and "GOOGLEAPPS_CLIENT_SECRET" in hints
    # No client yet — do not suggest sign-in (the consent button needs the client).
    assert "Hardy's Send panel" not in hints
    # The service does not read .env, and the hint has to say so or the user
    # edits a file the service never opens.
    assert "does not read .env" in hints


def test_config_when_configured_reports_ready_and_leaks_nothing(server, configured):
    status, body = get(server, "/deliver/config")
    assert status == 200
    assert body["chat"] is True and body["gmail"] is True and body["calendar"] is True
    assert body["oauth_client"] is True and body["signed_in"] is True
    assert body["token"] == "valid"
    assert body["hints"] == []
    text = json.dumps(body)
    assert "t-secret-token" not in text and "k-secret-key" not in text
    assert "client-secret-value" not in text
    assert "ya29." not in text


def test_deliver_auth_refuses_without_oauth_client(server):
    status, body = post(server, "/deliver/auth/start", {})
    assert status == 400
    assert "CLIENT" in body["error"].upper() or "oauth" in body["error"].lower()


def test_deliver_auth_start_returns_url_then_finish_reports_config(
    server, tmp_path, monkeypatch
):
    token_path = tmp_path / "google-token.json"
    monkeypatch.setenv("GOOGLEAPPS_CLIENT_ID", "client-id.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLEAPPS_CLIENT_SECRET", "client-secret-value")
    monkeypatch.setenv("GOOGLEAPPS_TOKEN_PATH", str(token_path))

    def fake_flow(config, _transport, **kwargs):
        on_url = kwargs.get("on_url")
        if on_url is not None:
            on_url("https://accounts.google.com/o/oauth2/v2/auth?client=test")
        save_token(config.token_path, valid_token())
        return config.token_path

    monkeypatch.setattr("googleapps.auth.run_auth_flow", fake_flow)
    status, body = post(server, "/deliver/auth/start", {})
    assert status == 200, body
    assert body["auth_url"].startswith("https://accounts.google.com/")

    status, body = post(server, "/deliver/auth", {"client_opens": True})
    assert status == 200, body
    assert body["signed_in"] is True
    assert body["gmail"] is True and body["calendar"] is True
    assert token_path.is_file()


def test_deliver_auth_all_in_one_still_works(server, tmp_path, monkeypatch):
    token_path = tmp_path / "google-token.json"
    monkeypatch.setenv("GOOGLEAPPS_CLIENT_ID", "client-id.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLEAPPS_CLIENT_SECRET", "client-secret-value")
    monkeypatch.setenv("GOOGLEAPPS_TOKEN_PATH", str(token_path))

    def fake_flow(config, _transport, **_kwargs):
        save_token(config.token_path, valid_token())
        return config.token_path

    monkeypatch.setattr("googleapps.auth.run_auth_flow", fake_flow)
    status, body = post(server, "/deliver/auth", {})
    assert status == 200, body
    assert body["signed_in"] is True
    assert token_path.is_file()


def test_config_oauth_without_token_points_at_ada(server, tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLEAPPS_CLIENT_ID", "client-id.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLEAPPS_CLIENT_SECRET", "client-secret-value")
    monkeypatch.setenv("GOOGLEAPPS_TOKEN_PATH", str(tmp_path / "no-token.json"))
    status, body = get(server, "/deliver/config")
    assert status == 200
    assert body["oauth_client"] is True
    assert body["signed_in"] is False
    assert body["gmail"] is False
    hints = "\n".join(body["hints"])
    assert "Hardy's Send panel" in hints
    assert "python -m googleapps auth" in hints


# ---------------------------------------------------------------- delivery


def test_chat_and_email_deliver_the_routed_board(server, tmp_path, configured):
    routed_session(
        tmp_path,
        unrouted={"VOUT": "no path at 0.25 mm clearance"},
        findings=[finding(Severity.NOTE, title="add a test point on VOUT")],
    )
    status, body = post(
        server,
        "/steps/sess1/deliver",
        {"chat": True, "email": "lead@example.com, fab@example.com"},
    )
    assert status == 200, body
    assert body["chat"] == {"ok": True}
    assert body["email"]["ok"] is True
    assert body["email"]["message_id"] == "msg-1"
    assert body["email"]["to"] == ["lead@example.com", "fab@example.com"]

    # The card names the unrouted net and the router's reason, verbatim.
    (card,) = configured.bodies("chat.googleapis.com")
    text = json.dumps(card)
    assert "unrouted VOUT: no path at 0.25 mm clearance" in text
    assert "1 net(s) left unrouted" in card["text"]
    assert "2 parts, 2 nets" in text

    # The email carries the .kicad_pcb, byte for byte.
    (sent,) = configured.bodies("gmail.googleapis.com")
    message = email.message_from_bytes(
        base64.urlsafe_b64decode(sent["raw"]), policy=email.policy.default
    )
    assert message["To"] == "lead@example.com, fab@example.com"
    assert "a-3-3v-regulator" in message["Subject"]
    attachments = list(message.iter_attachments())
    assert [a.get_filename() for a in attachments] == ["a-3-3v-regulator.kicad_pcb"]
    assert attachments[0].get_payload(decode=True) == BOARD_BYTES
    assert "VOUT: no path at 0.25 mm clearance" in message.get_body().get_content()

    # Secrets stay out of the response.
    text = json.dumps(body)
    assert "t-secret-token" not in text and "ya29." not in text


def test_schedule_is_skipped_without_blockers_and_says_so(server, tmp_path, configured):
    routed_session(tmp_path, findings=[finding(Severity.NOTE)])
    status, body = post(
        server,
        "/steps/sess1/deliver",
        {"schedule": True, "attendees": ["lead@example.com"]},
    )
    assert status == 200, body
    assert body["calendar"] == {
        "ok": False,
        "skipped_reason": "no blockers — nothing scheduled",
    }
    assert not configured.called("calendar/v3")


def test_schedule_without_a_review_does_not_claim_a_clean_board(
    server, tmp_path, configured
):
    routed_session(tmp_path, reviewed=False)
    status, body = post(
        server,
        "/steps/sess1/deliver",
        {"schedule": True, "attendees": ["lead@example.com"]},
    )
    assert status == 200, body
    assert body["calendar"]["ok"] is False
    assert "review step has not run" in body["calendar"]["skipped_reason"]
    assert not configured.called("calendar/v3")


def test_schedule_with_blockers_creates_the_review_and_invites(
    server, tmp_path, configured
):
    routed_session(
        tmp_path,
        findings=[finding(title="VIN has no bulk capacitor"), finding(Severity.NOTE)],
    )
    status, body = post(
        server,
        "/steps/sess1/deliver",
        {"schedule": True, "attendees": ["lead@example.com"]},
    )
    assert status == 200, body
    assert body["calendar"]["ok"] is True
    assert body["calendar"]["blockers"] == 1
    assert body["calendar"]["meet_uri"] == "https://meet.google.com/abc-defg-hij"
    assert body["calendar"]["html_link"].startswith("https://calendar.google.com/")
    (request,) = [r for r in configured.requests if "calendar/v3" in r.url]
    assert "sendUpdates=all" in request.url
    event = json.loads(request.body)
    assert event["attendees"] == [{"email": "lead@example.com"}]
    assert "VIN has no bulk capacitor" in event["description"]


def test_destinations_fail_independently(server, tmp_path, configured, monkeypatch):
    """A refused webhook must not stop the email, and the response says which."""
    monkeypatch.setenv("GOOGLEAPPS_CHAT_WEBHOOK", "https://evil.example/v1/spaces/x")
    routed_session(tmp_path)
    status, body = post(
        server, "/steps/sess1/deliver", {"chat": True, "email": ["lead@example.com"]}
    )
    assert status == 200, body
    assert body["chat"]["ok"] is False
    assert "chat.googleapis.com" in body["chat"]["error"]
    assert body["email"]["ok"] is True
    assert not configured.called("chat.googleapis.com")


# ---------------------------------------------------------------- refusals


def test_a_bad_address_is_a_400_before_any_request(server, tmp_path, configured):
    routed_session(tmp_path)
    status, body = post(
        server,
        "/steps/sess1/deliver",
        {"chat": True, "email": ["lead@example.com", "not an address"]},
    )
    assert status == 400
    assert "not an address" in body["error"]
    assert configured.requests == []


def test_nothing_requested_is_a_400(server, tmp_path, configured):
    routed_session(tmp_path)
    status, body = post(server, "/steps/sess1/deliver", {})
    assert status == 400 and "nothing to deliver" in body["error"]
    status, body = post(server, "/steps/sess1/deliver", {"schedule": True})
    assert status == 400 and "attendees" in body["error"]
    status, body = post(server, "/steps/sess1/deliver", {"chat": "yes"})
    assert status == 400 and "'chat'" in body["error"]


def test_an_unrouted_session_is_a_409(server, tmp_path, configured):
    routed_session(tmp_path, stage="placed")
    status, body = post(server, "/steps/sess1/deliver", {"chat": True})
    assert status == 409
    assert "needs the run to be 'routed'" in body["error"]
    assert configured.requests == []


def test_an_unknown_session_is_a_404(server, configured):
    status, body = post(server, "/steps/nope/deliver", {"chat": True})
    assert status == 404 and "nope" in body["error"]


def test_unconfigured_destinations_report_the_hint_not_a_traceback(
    server, tmp_path
):
    transport = RecordingTransport()
    deliver.transport_factory = lambda: transport
    routed_session(tmp_path)
    status, body = post(
        server, "/steps/sess1/deliver", {"chat": True, "email": ["lead@example.com"]}
    )
    assert status == 200, body
    assert body["chat"]["ok"] is False
    assert "GOOGLEAPPS_CHAT_WEBHOOK" in body["chat"]["error"]
    assert body["email"]["ok"] is False
    assert "python -m googleapps auth" in body["email"]["error"]
    assert transport.requests == []


# ---------------------------------------------------------------- real session


def test_a_real_step_session_delivers_after_routing(server, configured):
    """The adapter against a genuine session: scripted propose, real place and route."""
    status, body = post(
        server, "/steps", {"intent": "build me a toy car", "time_limit_s": 5}
    )
    assert status == 200, body
    sid = body["session"]
    status, body = post(server, f"/steps/{sid}/deliver", {"chat": True})
    assert status == 409, body

    assert post(server, f"/steps/{sid}/place", {})[0] == 200
    status, routed = post(server, f"/steps/{sid}/route", {})
    assert status == 200, routed

    status, body = post(server, f"/steps/{sid}/deliver", {"chat": True})
    assert status == 200, body
    assert body["chat"] == {"ok": True}
    (card,) = configured.bodies("chat.googleapis.com")
    text = json.dumps(card)
    assert "build me a toy car" in text
    for net in routed["routing"]["unrouted"]:
        assert f"unrouted {net}:" in text


# ---------------------------------------------------------------- the auth job
#
# The Setup Assistant polls rather than blocks, so the job has a state it can
# read without calling finish_auth, and it survives completion so a poll that
# lands after the redirect still sees "connected".


@pytest.fixture
def oauth_client(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLEAPPS_CLIENT_ID", "client-id.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLEAPPS_CLIENT_SECRET", "client-secret-value")
    monkeypatch.setenv("GOOGLEAPPS_TOKEN_PATH", str(tmp_path / "google-token.json"))
    deliver.cancel_auth()
    yield tmp_path / "google-token.json"
    deliver.cancel_auth()


def test_auth_status_is_idle_then_waiting_then_connected_without_finish(
    oauth_client, monkeypatch
):
    assert deliver.auth_status() == {"state": "idle", "error": None, "auth_url": None}
    release = threading.Event()

    def fake_flow(config, _transport, **kwargs):
        kwargs["on_url"]("https://accounts.google.com/o/oauth2/v2/auth?client=test")
        release.wait(5)
        save_token(config.token_path, valid_token())
        return config.token_path

    monkeypatch.setattr("googleapps.auth.run_auth_flow", fake_flow)
    deliver.begin_auth()
    status = deliver.auth_status()
    assert status["state"] == "waiting" and status["error"] is None
    assert status["auth_url"].startswith("https://accounts.google.com/")
    release.set()
    with deliver._auth_lock:
        job = deliver._auth_job
    assert job.done.wait(5)
    assert deliver.auth_status()["state"] == "connected"
    # finish_auth still works, and works twice: the job is kept.
    first = deliver.finish_auth()
    assert first["signed_in"] is True
    assert deliver.finish_auth() == first
    assert deliver.auth_status()["state"] == "connected"
    assert "ya29." not in json.dumps(deliver.auth_status())


def test_auth_status_reports_failed_with_the_reason(oauth_client, monkeypatch):
    from googleapps.auth import AuthError

    def refuse(config, _transport, **kwargs):
        kwargs["on_url"]("https://accounts.google.com/o/oauth2/v2/auth?client=test")
        raise AuthError("the user declined consent")

    monkeypatch.setattr("googleapps.auth.run_auth_flow", refuse)
    deliver.begin_auth()
    with deliver._auth_lock:
        job = deliver._auth_job
    assert job.done.wait(5)
    status = deliver.auth_status()
    assert status["state"] == "failed" and "declined" in status["error"]
    with pytest.raises(ValueError, match="declined"):
        deliver.finish_auth()
    # A failed job does not block the next attempt.
    assert deliver.cancel_auth() == {"cancelled": True}
    assert deliver.auth_status()["state"] == "idle"


def test_sign_out_deletes_the_token_here_and_says_it_is_not_revoked(oauth_client):
    save_token(oauth_client, valid_token())
    body = deliver.sign_out()
    assert body["signed_out"] is True and body["removed"] is True
    assert not oauth_client.exists()
    assert body["note"] == deliver.SIGN_OUT_NOTE
    assert "not revoked at Google" in body["note"]
    assert "myaccount.google.com/permissions" in body["note"]
    assert body["report"]["signed_in"] is False
    # Idempotent: nothing to remove is still a sign-out, not an error.
    assert deliver.sign_out()["removed"] is False


# ---------------------------------------------------------- failed critic


def test_a_failed_critic_is_refused_everywhere_with_the_failed_sentence(
    server, tmp_path, configured
):
    """``reviewed`` is true for a critic whose answer could not be read (the
    step ran), and until 2026-09-06 that fell into "no blockers -- nothing
    scheduled": a model failure read as a clean board, and a card and an
    email that said so. Now every destination reads the ``ReviewReport`` and
    says the one sentence ``googleapps.chat.verdict`` uses, and no meeting is
    booked off it -- even when the review step stored an agenda with a
    blocking item, because that agenda was built from evidence the critic
    never weighed in on."""
    from silkscreen.specreview import AgendaItem, SpecReview, SpecReviewResult

    stored = SpecReviewResult(
        review=SpecReview(
            title="Spec review",
            summary="One open question.",
            items=(
                AgendaItem(
                    topic="VOUT left unrouted", why="no path", minutes=15,
                    blocking=True, refs=("VOUT",),
                ),
            ),
        )
    )
    routed_session(
        tmp_path,
        review=failed_review(),
        unrouted={"VOUT": "no path at 0.25 mm clearance"},
        spec_review=stored,
    )
    status, body = post(
        server,
        "/steps/sess1/deliver",
        {
            "chat": True,
            "email": ["lead@example.com"],
            "schedule": True,
            "spec_review": True,
            "attendees": ["lead@example.com"],
        },
    )
    assert status == 200, body
    failed = "not reviewed — the review failed"

    assert body["calendar"]["ok"] is False
    assert body["calendar"]["skipped_reason"].startswith(failed)
    assert "no blockers" not in body["calendar"]["skipped_reason"]
    assert body["spec_review"]["ok"] is False
    assert body["spec_review"]["skipped_reason"].startswith(failed)
    assert "no blocking item" not in body["spec_review"]["skipped_reason"]
    assert not configured.called("calendar/v3"), "no meeting over a failed critic"

    assert body["chat"] == {"ok": True}
    (card,) = configured.bodies("chat.googleapis.com")
    text = json.dumps(card)
    assert failed in card["text"]
    assert "no findings" not in text and "board ready" not in text

    assert body["email"]["ok"] is True
    (sent,) = configured.bodies("gmail.googleapis.com")
    message = email.message_from_bytes(
        base64.urlsafe_b64decode(sent["raw"]), policy=email.policy.default
    )
    assert failed in message["Subject"]
    content = message.get_body().get_content()
    assert failed in content
    assert "no findings" not in content
    assert "readable answer" in content, "the critic's own note is in the body"


def test_a_failed_critic_with_no_stored_agenda_is_refused_the_same_way(
    server, tmp_path, configured
):
    routed_session(tmp_path, review=failed_review())
    status, body = post(
        server,
        "/steps/sess1/deliver",
        {"schedule": True, "spec_review": True, "attendees": ["lead@example.com"]},
    )
    assert status == 200, body
    assert body["calendar"]["skipped_reason"].startswith(
        "not reviewed — the review failed"
    )
    assert body["spec_review"]["skipped_reason"].startswith(
        "not reviewed — the review failed"
    )
    assert not configured.called("calendar/v3")


def test_an_ok_report_with_no_blockers_still_says_no_blockers(
    server, tmp_path, configured
):
    """The other direction: a critic that answered and found nothing is the
    one case that may say "no blockers"."""
    routed_session(
        tmp_path,
        review=ReviewReport(status=ReviewStatus.OK),
        findings=[finding(Severity.NOTE)],
    )
    status, body = post(
        server,
        "/steps/sess1/deliver",
        {"schedule": True, "spec_review": True, "attendees": ["lead@example.com"]},
    )
    assert status == 200, body
    assert body["calendar"]["skipped_reason"] == "no blockers — nothing scheduled"
    assert "no blocking item" in body["spec_review"]["skipped_reason"]


def test_the_stored_agenda_is_used_once_the_critic_answered(
    server, tmp_path, configured
):
    from silkscreen.specreview import AgendaItem, SpecReview, SpecReviewResult

    stored = SpecReviewResult(
        review=SpecReview(
            title="Spec review",
            summary="One open question.",
            items=(
                AgendaItem(
                    topic="Regulator thermal budget", why="unbounded", minutes=20,
                    blocking=True, refs=("U1",),
                ),
            ),
        )
    )
    routed_session(
        tmp_path, review=ReviewReport(status=ReviewStatus.OK), spec_review=stored
    )
    status, body = post(
        server,
        "/steps/sess1/deliver",
        {"spec_review": True, "attendees": ["lead@example.com"]},
    )
    assert status == 200, body
    assert body["spec_review"]["ok"] is True, body
    assert body["spec_review"]["blocking"] == 1
    (request,) = [r for r in configured.requests if "calendar/v3" in r.url]
    assert "Regulator thermal budget" in json.loads(request.body)["description"]


# ------------------------------------------------- granular consent

GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.send"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"


@pytest.fixture
def scoped(tmp_path, monkeypatch, configured):
    """Rewrite the configured token with an explicit grant record."""

    def write(*scopes):
        save_token(
            tmp_path / "google-token.json", valid_token(scopes=list(scopes))
        )

    return write


def test_config_closes_only_the_destination_whose_box_was_unticked(
    server, scoped
):
    """Granular consent lets a user grant Calendar and decline Gmail. A panel
    that offers both then fails at send time, after the run is paid for."""
    scoped(CALENDAR_SCOPE)
    _, body = get(server, "/deliver/config")
    assert body["signed_in"] is True
    assert body["calendar"] is True and body["spec_review"] is True
    assert body["gmail"] is False
    assert body["scopes"] == [CALENDAR_SCOPE]
    assert body["scopes_recorded"] is True
    hints = "\n".join(body["hints"])
    assert GMAIL_SCOPE in hints
    assert "python -m googleapps auth" in hints


def test_config_reports_a_full_grant_as_all_three_usable(server, scoped):
    scoped(GMAIL_SCOPE, CALENDAR_SCOPE)
    _, body = get(server, "/deliver/config")
    assert body["gmail"] is True
    assert body["calendar"] is True
    assert body["spec_review"] is True
    assert body["hints"] == []


def test_config_calls_an_unrecorded_grant_unrecorded_not_denied(
    server, configured
):
    """The token the ``configured`` fixture writes predates the record. It is
    reported usable -- refusing on no evidence would break a working sign-in
    to fix a hypothetical one -- and says the record is absent."""
    _, body = get(server, "/deliver/config")
    assert body["gmail"] is True and body["calendar"] is True
    assert body["scopes_recorded"] is False
    assert body["scopes"] == []


def test_a_declined_scope_fails_only_its_own_destination(
    server, tmp_path, scoped
):
    """The per-destination rule the module already keeps for a bad webhook,
    applied to a scope: Calendar still books, Gmail refuses in words that
    name the checkbox."""
    scoped(CALENDAR_SCOPE)
    routed_session(
        tmp_path,
        findings=[finding(Severity.BLOCKER, "VIN has no bulk capacitor")],
        review=ReviewReport(status=ReviewStatus.OK),
    )
    status, body = post(
        server,
        "/steps/sess1/deliver",
        {
            "email": ["a@example.com"],
            "schedule": True,
            "attendees": ["lead@example.com"],
        },
    )
    assert status == 200, body
    assert body["email"]["ok"] is False
    assert GMAIL_SCOPE in body["email"]["error"]
    assert body["calendar"]["ok"] is True, body


def test_integrations_inherits_the_scope_verdict(server, scoped):
    """``/integrations`` reads ``config_report`` rather than re-deriving, so a
    declined scope must show there as partial rather than ready."""
    scoped(CALENDAR_SCOPE)
    _, body = get(server, "/integrations")
    entry = next(e for e in body["integrations"] if e["id"] == "google")
    assert entry["state"] == "partial"
    assert "Gmail" not in entry["detail"]
    assert GMAIL_SCOPE in "\n".join(entry["hints"])

"""The Teams HTTP surface: authenticity, replay, gates, and acknowledgement.

Named ``test_teams_http`` rather than ``test_app`` because
``scripts/check_docs.py`` keys its per-module test counts by file *basename*,
and a second ``test_app.py`` would be silently folded into another package's
count -- the same reason ``slackbot/tests/test_http.py`` has its name.

Offline: no network, no Microsoft, no credentials. The tokens here are signed
with a key pair this file generates, so the real RS256 verification path runs
against real signatures rather than a monkeypatched check.
"""

from __future__ import annotations

import base64
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from teamsbot.app import (
    ACCEPTED_ISSUERS,
    CALLING_ISSUERS,
    DEFAULT_PORT,
    MAX_BODY_BYTES,
    MISSING_RUN_NOTE,
    AuthError,
    Dispatcher,
    Incoming,
    OpenIdKeys,
    RunMemory,
    RunRecord,
    StaticKeys,
    ensure_key_url,
    incoming_from_activity,
    incoming_from_calls,
    make_handler,
    make_server,
    verify_notification,
)
from teamsbot.config import DEFAULT_PORT as CONFIG_DEFAULT_PORT
from teamsbot.config import Config

cryptography = pytest.importorskip(
    "cryptography",
    reason="RS256 verification needs the cryptography package, which is also "
    "what the endpoint itself requires: without it every request is refused.",
)

from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402

APP_ID = "11111111-2222-3333-4444-555555555555"
MEETING = "19:meeting_abc123@thread.v2"

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KEYS = StaticKeys({"k1": KEY.public_key()})


def config(**overrides) -> Config:
    values = {
        "app_id": APP_ID,
        "app_secret": "s3cr3t-value",
        "tenant_id": "66666666-7777-8888-9999-000000000000",
        "speak_mode": "off",
    }
    values.update(overrides)
    return Config(**values)


CONFIG = config()


# -- token minting ---------------------------------------------------------


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def token(*, key=KEY, kid="k1", alg="RS256", sign=True, **claim_overrides) -> str:
    now = time.time()
    claims = {
        "iss": ACCEPTED_ISSUERS[0],
        "aud": APP_ID,
        "iat": now - 5,
        "nbf": now - 5,
        "exp": now + 600,
    }
    claims.update(claim_overrides)
    header = b64(json.dumps({"alg": alg, "typ": "JWT", "kid": kid}).encode())
    payload = b64(json.dumps(claims).encode())
    signing_input = f"{header}.{payload}".encode()
    if not sign:
        return f"{header}.{payload}."
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{payload}.{b64(signature)}"


class Headers(dict):
    """The subset of ``http.client.HTTPMessage`` the verifier touches."""


def auth(value: str) -> Headers:
    return Headers({"Authorization": value})


# -- fakes -----------------------------------------------------------------


class FakeRunner:
    """Records what would have been run, and runs nothing."""

    def __init__(self, outcome=None, raises=None):
        self.handled: list[Incoming] = []
        self.outcome = outcome
        self.raises = raises
        self.started = threading.Event()

    def handle(self, incoming: Incoming):
        self.handled.append(incoming)
        self.started.set()
        if self.raises is not None:
            raise self.raises
        return self.outcome


class FakeSpeaker:
    name = "test"

    def __init__(self):
        self.said: list[tuple[str, str]] = []

    def say(self, meeting_id, text):
        self.said.append((meeting_id, text))


@pytest.fixture
def dispatcher():
    # A short slot wait: the production 240s exists to absorb a queued run, and
    # waiting it out would make this file take minutes.
    return Dispatcher(
        CONFIG, FakeRunner(), speaker=FakeSpeaker(), slot_wait_s=0.05
    )


# -- payload builders ------------------------------------------------------


def activity(text="design a 3v3 rail", **overrides):
    payload = {
        "type": "message",
        "id": "activity-1",
        "text": text,
        "from": {"id": "user-9"},
        "recipient": {"id": "bot-1"},
        "conversation": {"id": MEETING},
        "channelData": {"meeting": {"id": MEETING}},
    }
    payload.update(overrides)
    return payload


def call_notification(**overrides):
    entry = {
        "changeType": "created",
        "resource": "/communications/calls/call-1",
        "resourceData": {"state": "incoming", "chatInfo": {"threadId": MEETING}},
    }
    entry.update(overrides)
    return {"value": [entry]}


# -- verification ----------------------------------------------------------


def test_a_valid_token_verifies_and_returns_its_claims():
    claims = verify_notification(CONFIG, auth(f"Bearer {token()}"), keys=KEYS)
    assert claims["aud"] == APP_ID


@pytest.mark.parametrize(
    "headers, code",
    [
        (Headers(), "missing_token"),
        (auth("Basic abc"), "missing_token"),
        (auth("Bearer not-a-jwt"), "malformed_token"),
    ],
)
def test_a_request_with_no_usable_token_is_refused(headers, code):
    with pytest.raises(AuthError) as exc:
        verify_notification(CONFIG, headers, keys=KEYS)
    assert exc.value.code == code


def test_an_unsigned_alg_none_token_is_refused():
    # The classic forgery: the attacker picks the algorithm, and a verifier
    # that honours the choice checks a signature it can trivially produce.
    with pytest.raises(AuthError) as exc:
        verify_notification(
            CONFIG, auth(f"Bearer {token(alg='none', sign=False)}"), keys=KEYS
        )
    assert exc.value.code == "bad_algorithm"


def test_a_token_signed_by_the_wrong_key_is_refused():
    with pytest.raises(AuthError) as exc:
        verify_notification(CONFIG, auth(f"Bearer {token(key=OTHER_KEY)}"), keys=KEYS)
    assert exc.value.code == "bad_signature"


def test_an_unknown_kid_is_refused():
    with pytest.raises(AuthError) as exc:
        verify_notification(CONFIG, auth(f"Bearer {token(kid='k9')}"), keys=KEYS)
    assert exc.value.code == "unknown_kid"


@pytest.mark.parametrize(
    "overrides, code",
    [
        ({"aud": "someone-elses-bot"}, "bad_audience"),
        ({"iss": "https://evil.example"}, "bad_issuer"),
        # Offsets, not timestamps: a parametrize list is evaluated at collection
        # and the full suite takes ~25 minutes to reach this test, so a
        # collection-time `time.time()` drifts across the CLOCK_SKEW_S window
        # and the refusal comes back with a different code (seen 2026-09-06).
        ({"exp": -3600}, "expired"),
        ({"nbf": +3600}, "not_yet_valid"),
        ({"iat": -90000}, "stale"),
    ],
)
def test_a_token_outside_its_window_or_meant_for_another_app_is_refused(
    overrides, code
):
    now = time.time()
    claims = {
        key: (now + value if isinstance(value, (int, float)) else value)
        for key, value in overrides.items()
    }
    with pytest.raises(AuthError) as exc:
        verify_notification(CONFIG, auth(f"Bearer {token(**claims)}"), keys=KEYS)
    assert exc.value.code == code


def test_a_token_with_no_expiry_is_refused():
    minted = token()
    header, payload, signature = minted.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    claims.pop("exp")
    stripped = f"{header}.{b64(json.dumps(claims).encode())}.{signature}"
    with pytest.raises(AuthError) as exc:
        verify_notification(CONFIG, auth(f"Bearer {stripped}"), keys=KEYS)
    assert exc.value.code == "expired"


def test_no_key_source_refuses_rather_than_waving_the_token_through():
    with pytest.raises(AuthError) as exc:
        verify_notification(CONFIG, auth(f"Bearer {token()}"), keys=None)
    assert exc.value.code == "no_key_source"


# -- key discovery ---------------------------------------------------------


def jwks_entry(key, kid):
    numbers = key.public_key().public_numbers()
    size = (numbers.n.bit_length() + 7) // 8
    return {
        "kty": "RSA",
        "kid": kid,
        "n": b64(numbers.n.to_bytes(size, "big")),
        "e": b64(numbers.e.to_bytes(3, "big")),
    }


def test_published_keys_are_fetched_and_used():
    documents = {
        "https://login.botframework.com/v1/.well-known/openidconfiguration": {
            "jwks_uri": "https://login.botframework.com/v1/.well-known/keys"
        },
        "https://login.botframework.com/v1/.well-known/keys": {
            "keys": [jwks_entry(KEY, "k1")]
        },
    }
    fetched: list[str] = []

    def fetch(url):
        fetched.append(url)
        return json.dumps(documents[url]).encode()

    keys = OpenIdKeys(
        fetch=fetch,
        configuration_urls=(
            "https://login.botframework.com/v1/.well-known/openidconfiguration",
        ),
    )
    claims = verify_notification(CONFIG, auth(f"Bearer {token()}"), keys=keys)
    assert claims["iss"] == ACCEPTED_ISSUERS[0]
    # Cached: a second verification does not re-fetch.
    verify_notification(CONFIG, auth(f"Bearer {token()}"), keys=keys)
    assert len(fetched) == 2


def test_a_metadata_document_cannot_redirect_key_discovery_off_the_allowlist():
    def fetch(url):
        return json.dumps({"jwks_uri": "https://keys.evil.example/jwks"}).encode()

    keys = OpenIdKeys(
        fetch=fetch,
        configuration_urls=(
            "https://login.botframework.com/v1/.well-known/openidconfiguration",
        ),
    )
    with pytest.raises(AuthError) as exc:
        keys.public_key("k1")
    assert exc.value.code == "bad_host"


@pytest.mark.parametrize(
    "url",
    [
        "http://login.botframework.com/v1/.well-known/keys",
        "https://login.botframework.com.evil.example/keys",
        "https://keys.example/jwks",
    ],
)
def test_key_urls_are_allowlisted_by_exact_host_over_https(url):
    with pytest.raises(AuthError) as exc:
        ensure_key_url(url)
    assert exc.value.code == "bad_host"


# -- parsing ---------------------------------------------------------------


def test_an_activity_becomes_one_incoming_with_the_mention_stripped():
    incoming = incoming_from_activity(activity(text="<at>Kaleo</at> design a rail"))
    assert incoming.kind == "message"
    assert incoming.text == "design a rail"
    assert incoming.meeting_id == MEETING


def test_the_bots_own_message_is_not_a_request():
    # Two bots in one meeting chat would otherwise answer each other forever.
    assert incoming_from_activity(activity(**{"from": {"id": "bot-1"}})) is None
    assert incoming_from_activity({"type": "typing"}) is None


def test_a_call_notification_without_an_id_gets_a_deterministic_one():
    first = incoming_from_calls(call_notification())[0]
    second = incoming_from_calls(call_notification())[0]
    # A retry of the same notification must derive the same id, or dedupe
    # cannot work and every retry becomes another paid run.
    assert first.notification_id == second.notification_id
    assert first.notification_id.startswith("sha256:")
    assert first.meeting_id == MEETING
    other = incoming_from_calls(call_notification(changeType="deleted"))[0]
    assert other.notification_id != first.notification_id


def test_a_malformed_entry_does_not_discard_the_rest_of_the_notification():
    payload = {"value": ["nonsense", call_notification()["value"][0]]}
    assert len(incoming_from_calls(payload)) == 1
    assert incoming_from_calls({"value": "nope"}) == []


# -- dispatch --------------------------------------------------------------


def test_an_activity_is_accepted_once_however_often_it_is_delivered(dispatcher):
    code, _ = dispatcher.handle_activity_payload(activity())
    assert code == 202
    again, body = dispatcher.handle_activity_payload(activity())
    dispatcher.join(timeout=5)
    assert again == 200 and body["accepted"] == 0
    # One paid run for one request, which is the entire point of the id memory.
    assert len(dispatcher.runner.handled) == 1


def test_a_replayed_call_notification_does_not_start_a_second_run(dispatcher):
    dispatcher.handle_call_payload(call_notification())
    dispatcher.handle_call_payload(call_notification())
    dispatcher.join(timeout=5)
    assert len(dispatcher.runner.handled) == 1


def test_a_meeting_outside_the_allowlist_is_ignored():
    dispatcher = Dispatcher(
        config(meeting_allowlist=("19:other@thread.v2",)),
        FakeRunner(),
        speaker=FakeSpeaker(),
        slot_wait_s=0.05,
    )
    code, body = dispatcher.handle_activity_payload(activity())
    dispatcher.join(timeout=5)
    assert (code, body["accepted"]) == (200, 0)
    assert dispatcher.runner.handled == []


def test_a_follow_up_after_a_restart_says_so_instead_of_guessing(dispatcher):
    # Run memory is in-process; a restart loses it. Answering "review" from
    # whatever board is lying around is the failure this sentence prevents.
    dispatcher.handle_activity_payload(activity(text="review"))
    dispatcher.join(timeout=5)
    assert dispatcher.runner.handled == []
    assert dispatcher.speaker.said == [(MEETING, MISSING_RUN_NOTE)]


def test_a_follow_up_reaches_the_runner_once_that_meeting_has_a_run(dispatcher):
    dispatcher.memory.remember(RunRecord(meeting_id=MEETING, request="a rail"))
    dispatcher.handle_activity_payload(activity(text="review please"))
    dispatcher.join(timeout=5)
    assert dispatcher.runner.handled[0].follow_up == "review"
    assert dispatcher.speaker.said == []


def test_a_finished_run_is_remembered_for_the_meeting(dispatcher):
    dispatcher.handle_activity_payload(activity())
    dispatcher.join(timeout=5)
    record = dispatcher.memory.recall(MEETING)
    assert record is not None and record.request == "design a 3v3 rail"


def test_a_failed_run_is_reported_and_not_remembered():
    dispatcher = Dispatcher(
        CONFIG,
        FakeRunner(raises=RuntimeError("the solver exploded")),
        speaker=FakeSpeaker(),
        slot_wait_s=0.05,
    )
    dispatcher.handle_activity_payload(activity())
    dispatcher.join(timeout=5)
    # Nothing to refer back to, and the meeting was told rather than left
    # waiting on an answer that is never coming.
    assert dispatcher.memory.recall(MEETING) is None
    assert dispatcher.speaker.said and "failed" in dispatcher.speaker.said[0][1]


def test_run_memory_is_bounded():
    memory = RunMemory(limit=2)
    for index in range(3):
        memory.remember(RunRecord(meeting_id=f"m{index}", request="x"))
    assert memory.recall("m0") is None
    assert memory.recall("m2") is not None


# -- over HTTP -------------------------------------------------------------


@pytest.fixture
def server(dispatcher):
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0), make_handler(dispatcher, keys=KEYS)
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd, dispatcher
    httpd.shutdown()
    httpd.server_close()


def post(httpd, path, body: bytes, *, bearer: str | None = "valid", headers=None):
    request_headers = {"Content-Type": "application/json"}
    request_headers.update(headers or {})
    if bearer is not None:
        request_headers["Authorization"] = (
            f"Bearer {token()}" if bearer == "valid" else bearer
        )
    request = urllib.request.Request(
        f"http://127.0.0.1:{httpd.server_port}{path}",
        data=body,
        headers=request_headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_healthz_names_the_service(server):
    httpd, _ = server
    with urllib.request.urlopen(
        f"http://127.0.0.1:{httpd.server_port}/healthz", timeout=5
    ) as response:
        assert json.loads(response.read()) == {
            "ok": True,
            "service": "silkscreen-teams",
        }


def test_an_unauthenticated_request_is_refused_and_never_parsed(server):
    httpd, dispatcher = server
    # Deliberately not JSON: a 400 here would prove the body reached a parser
    # before the token was checked.
    status, _ = post(httpd, "/api/messages", b"{not json", bearer=None)
    assert status == 401
    dispatcher.join(timeout=1)
    assert dispatcher.runner.handled == []


def test_a_forged_token_is_refused_over_http(server):
    httpd, dispatcher = server
    status, body = post(
        httpd,
        "/api/calls",
        json.dumps(call_notification()).encode(),
        bearer=f"Bearer {token(key=OTHER_KEY)}",
    )
    assert status == 401
    # The client is told nothing about which check failed.
    assert b"signature" not in body.lower()
    dispatcher.join(timeout=1)
    assert dispatcher.runner.handled == []


def test_an_authenticated_activity_is_acknowledged_and_worked_off_thread(server):
    httpd, dispatcher = server
    status, body = post(httpd, "/api/messages", json.dumps(activity()).encode())
    assert status == 202
    assert json.loads(body)["ok"] is True
    dispatcher.runner.started.wait(timeout=5)
    dispatcher.join(timeout=5)
    assert dispatcher.runner.handled[0].text == "design a 3v3 rail"


def test_an_authenticated_call_notification_is_acknowledged(server):
    httpd, dispatcher = server
    status, _ = post(httpd, "/api/calls", json.dumps(call_notification()).encode())
    assert status == 202
    dispatcher.join(timeout=5)
    assert dispatcher.runner.handled[0].kind == "call"


def test_a_replay_over_http_does_not_buy_a_second_run(server):
    httpd, dispatcher = server
    body = json.dumps(activity()).encode()
    assert post(httpd, "/api/messages", body)[0] == 202
    assert post(httpd, "/api/messages", body)[0] == 200
    dispatcher.join(timeout=5)
    assert len(dispatcher.runner.handled) == 1


def test_an_authenticated_request_with_a_bad_body_is_a_400(server):
    httpd, _ = server
    assert post(httpd, "/api/messages", b"{not json")[0] == 400
    assert post(httpd, "/api/messages", b"[1,2]")[0] == 400


def test_an_unknown_route_is_a_404(server):
    httpd, _ = server
    assert post(httpd, "/api/nope", b"{}")[0] == 404


def test_an_oversized_body_is_refused(server):
    httpd, dispatcher = server
    status, _ = post(httpd, "/api/messages", b"x" * (MAX_BODY_BYTES + 1))
    assert status == 413
    assert dispatcher.runner.handled == []


# -- which port the server binds -------------------------------------------


def test_the_module_does_not_define_a_second_default_port():
    # One definition. A server that binds a different port than the config
    # reports is a callback address registered with Microsoft that silently
    # never fires.
    assert DEFAULT_PORT is CONFIG_DEFAULT_PORT == 3978


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _serving(httpd):
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return thread


def test_make_server_binds_the_port_the_config_names():
    wanted = _free_port()
    httpd = make_server(
        config(port=wanted), FakeRunner(), speaker=FakeSpeaker(), keys=KEYS
    )
    _serving(httpd)
    try:
        assert httpd.server_port == wanted
        with urllib.request.urlopen(
            f"http://127.0.0.1:{wanted}/healthz", timeout=5
        ) as response:
            assert json.loads(response.read())["ok"] is True
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_an_explicit_port_argument_still_wins():
    # port=0 is how a test asks the OS for any free port; the config's port
    # must not override an argument the caller passed on purpose.
    httpd = make_server(
        config(port=_free_port()),
        FakeRunner(),
        speaker=FakeSpeaker(),
        keys=KEYS,
        port=0,
    )
    try:
        assert httpd.server_port != CONFIG_DEFAULT_PORT
        assert httpd.server_port > 0
    finally:
        httpd.server_close()


# -- the default runner: the production path must go through the gates ------
#
# ``_LazyRunner`` is what ``make_server`` wires in when no runner is injected,
# so it *is* the production path. It used to call ``runner.run_meeting``
# directly, stepping past every judgement ``runner.handle_incoming`` exists to
# make: the free ``order`` refusal was unreachable, so the word "order" in a
# meeting bought a paid Gemini call and an intent extraction instead of one
# sentence declining. Every test below fails against that old path.


class _NeverCalledModel:
    """Any use of this is a paid call that should not have happened."""

    def __init__(self):
        self.calls: list[str] = []

    def generate(self, prompt, **kwargs):  # pragma: no cover - must not run
        self.calls.append(prompt)
        raise AssertionError("a model call was made for a request that is refused")


def lazy_runner(monkeypatch, model=None, generate=None, **config_overrides):
    """The real ``_LazyRunner``, with the engine seams pinned offline."""
    from silkscreen.agents import model as model_module

    import teamsbot.app as app_module

    speaker = FakeSpeaker()
    monkeypatch.setattr(
        model_module, "default_model", lambda **kw: model or _NeverCalledModel()
    )
    if generate is not None:
        import silkscreen.agents as agents_module

        monkeypatch.setattr(agents_module, "generate_pcb", generate)
    return app_module._LazyRunner(config(**config_overrides), speaker), speaker


def test_the_default_runner_refuses_order_for_free(monkeypatch):
    """The `order` refusal must be reachable from the webhook, not dead code.

    Nothing may ever be ordered from a meeting, and refusing it must not cost
    a model call: the old path ran the whole intent extraction on the word
    "order" before deciding it could not order anything.
    """
    from teamsbot.runner import NO_ORDER_NOTE

    model = _NeverCalledModel()
    runner, speaker = lazy_runner(monkeypatch, model=model)
    report = runner.handle(
        Incoming(
            kind="message",
            notification_id="n1",
            meeting_id=MEETING,
            conversation_id="19:conv",
            text="order the boards please",
            sender="amira",
        )
    )
    assert model.calls == []
    assert report is not None and report.runs == []
    assert NO_ORDER_NOTE in report.warnings[0]
    # Said into the conversation, which is what a ChatSpeaker can address.
    assert speaker.said == [("19:conv", NO_ORDER_NOTE)]


def test_the_default_runner_answers_a_follow_up_without_a_paid_run(monkeypatch):
    from teamsbot.runner import FOLLOW_UP_NOTE

    model = _NeverCalledModel()
    runner, speaker = lazy_runner(monkeypatch, model=model)
    report = runner.handle(
        Incoming(
            kind="message",
            notification_id="n1",
            meeting_id=MEETING,
            conversation_id="19:conv",
            text="status of the last board",
            sender="amira",
        )
    )
    assert model.calls == []
    assert report.runs == []
    assert speaker.said == [("19:conv", FOLLOW_UP_NOTE)]


def _quoting_model(intent, quote, confidence=0.9):
    """A model that answers the intent extractor with one board request."""
    from silkscreen.agents.model import ScriptedModel

    return ScriptedModel(
        by_marker={
            "TRANSCRIPT": json.dumps(
                {
                    "requests": [
                        {
                            "intent": intent,
                            "quote": quote,
                            "confidence": confidence,
                        }
                    ]
                }
            )
        }
    )


class _RecordingGenerate:
    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, model, intent, **kwargs):
        self.calls.append(intent)
        return object()


def test_the_default_runner_applies_the_quote_gate(monkeypatch):
    """A request the model cannot quote from what was typed is dropped."""
    typed = "we should build a 3.3 volt regulator board"
    model = _quoting_model("A board nobody asked for", "a line nobody ever typed")
    generate = _RecordingGenerate()
    runner, _ = lazy_runner(monkeypatch, model=model, generate=generate)
    report = runner.handle(
        Incoming(
            kind="message",
            notification_id="n1",
            meeting_id=MEETING,
            conversation_id="19:conv",
            text=typed,
            sender="amira",
        )
    )
    assert generate.calls == []
    assert report.considered == []


def test_the_default_runner_records_a_below_floor_request_without_building(
    monkeypatch,
):
    typed = "we could just use a 5 volt rail"
    model = _quoting_model("A 5V rail board", typed, confidence=0.2)
    generate = _RecordingGenerate()
    runner, _ = lazy_runner(monkeypatch, model=model, generate=generate)
    report = runner.handle(
        Incoming(
            kind="message",
            notification_id="n1",
            meeting_id=MEETING,
            conversation_id="19:conv",
            text=typed,
            sender="amira",
        )
    )
    assert generate.calls == []
    assert len(report.considered) == 1
    assert "below the" in report.runs[0].skipped


def test_the_default_runner_honours_max_runs_per_meeting(monkeypatch):
    """The cap bounds paid runs, and the capped request is reported, not lost."""
    from silkscreen.agents.model import ScriptedModel

    typed = "build a 3v3 rail and also build a 5v rail"
    model = ScriptedModel(
        by_marker={
            "TRANSCRIPT": json.dumps(
                {
                    "requests": [
                        {
                            "intent": "A 3v3 rail",
                            "quote": "build a 3v3 rail",
                            "confidence": 0.9,
                        },
                        {
                            "intent": "A 5v rail",
                            "quote": "build a 5v rail",
                            "confidence": 0.9,
                        },
                    ]
                }
            )
        }
    )
    generate = _RecordingGenerate()
    runner, _ = lazy_runner(
        monkeypatch, model=model, generate=generate, max_runs_per_meeting=1
    )
    report = runner.handle(
        Incoming(
            kind="message",
            notification_id="n1",
            meeting_id=MEETING,
            conversation_id="19:conv",
            text=typed,
            sender="amira",
        )
    )
    assert generate.calls == ["A 3v3 rail"]
    assert len(report.considered) == 2
    assert "max_runs_per_meeting=1" in report.runs[1].skipped


def test_the_default_runner_reports_a_call_it_cannot_read(monkeypatch):
    """Dropping a call notification with a log line is a quiet no-op; the
    report says the transcript read needs a Graph client, and costs nothing."""
    model = _NeverCalledModel()
    runner, _ = lazy_runner(monkeypatch, model=model)
    report = runner.handle(
        Incoming(
            kind="call",
            notification_id="n1",
            meeting_id=MEETING,
            conversation_id="",
            change_type="created",
        )
    )
    assert model.calls == []
    assert report.runs == []
    assert "needs a GraphClient" in report.warnings[0]


def test_the_default_runner_still_ignores_a_message_with_no_text(monkeypatch):
    model = _NeverCalledModel()
    runner, _ = lazy_runner(monkeypatch, model=model)
    assert (
        runner.handle(
            Incoming(
                kind="message",
                notification_id="n1",
                meeting_id=MEETING,
                conversation_id="19:conv",
                text="   ",
            )
        )
        is None
    )
    assert model.calls == []


def test_the_production_path_is_handle_incoming_and_not_run_meeting():
    """A structural pin on the bypass itself.

    The gates live in ``handle_incoming``; a future edit that reaches around
    it into ``run_meeting`` again would restore the bug, and the behavioural
    tests above only catch the cases they happen to name.
    """
    import inspect

    from teamsbot.app import _LazyRunner

    source = inspect.getsource(_LazyRunner.handle)
    assert "handle_incoming" in source
    assert "run_meeting" not in source


# ---------------------------------------------------------------------------
# Issuers, pinned to what Microsoft actually publishes and actually validates
# ---------------------------------------------------------------------------


def test_the_activity_issuer_is_the_one_microsofts_sdks_declare():
    """`TO_BOT_FROM_CHANNEL_TOKEN_ISSUER` / `ToBotFromChannelTokenIssuer`.

    Also the `issuer` value both documents in OPENID_CONFIGURATION_URLS
    published when fetched on 2026-09-08.
    """
    assert ACCEPTED_ISSUERS == ("https://api.botframework.com",)


def test_the_calling_path_accepts_the_two_issuers_graph_actually_uses():
    """microsoft-graph-comms-samples AuthenticationProvider.cs `authIssuers`.

    Kept separate from ACCEPTED_ISSUERS on purpose — see CALLING_ISSUERS.
    """
    assert CALLING_ISSUERS == (
        "https://graph.microsoft.com",
        "https://api.botframework.com",
    )
    assert "https://graph.microsoft.com" not in ACCEPTED_ISSUERS


def test_a_graph_issued_notification_is_accepted_on_the_calling_route(server):
    """This was a 401 until 2026-09-08: /api/calls could never have worked."""
    httpd, _ = server
    graph_token = f"Bearer {token(iss='https://graph.microsoft.com')}"
    status, _ = post(
        httpd,
        "/api/calls",
        json.dumps(call_notification()).encode(),
        bearer=graph_token,
    )
    assert status != 401


def test_a_graph_issued_token_is_still_refused_on_the_activity_route(server):
    """The wider list is per route. Widening one must not widen the other."""
    httpd, dispatcher = server
    graph_token = f"Bearer {token(iss='https://graph.microsoft.com')}"
    status, _ = post(
        httpd,
        "/api/messages",
        json.dumps({"type": "message", "text": "hi"}).encode(),
        bearer=graph_token,
    )
    assert status == 401
    assert dispatcher.runner.handled == []


def test_an_unknown_issuer_is_refused_on_both_routes(server):
    """Neither list is a wildcard, and a refusal names the issuer in the log."""
    httpd, _ = server
    forged = f"Bearer {token(iss='https://graph.microsoft.com.evil.example')}"
    for path, body in (
        ("/api/calls", call_notification()),
        ("/api/messages", {"type": "message", "text": "hi"}),
    ):
        status, _ = post(httpd, path, json.dumps(body).encode(), bearer=forged)
        assert status == 401, path

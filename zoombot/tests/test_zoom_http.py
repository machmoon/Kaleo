"""The webhook surface: signatures, the handshake, acknowledgement, replays.

Named ``test_zoom_http.py`` rather than ``test_app.py`` because
``scripts/check_docs.py`` keys its per-module test counts by file *basename*,
and a second ``test_app.py`` would be silently folded into another package's
count -- the same reason ``slackbot/tests/test_http.py`` has its name.

Signatures are computed here with ``hmac``/``hashlib`` directly rather than by
calling the module under test: a check written in terms of the code it checks
shares that code's blind spot.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from zoombot.app import Dispatcher, make_server
from zoombot.config import Config
from zoombot.runner import NO_RUN_REMEMBERED, ZoomReport

SECRET = "webhook-secret-token"
NOW = 1_700_000_000.0

CONFIG = Config(
    client_id="client-id",
    client_secret="client-secret",
    account_id="account-id",
    webhook_secret=SECRET,
    speak_mode="off",
)


def sign(body: bytes, timestamp: str, secret: str = SECRET) -> str:
    base = f"v0:{timestamp}:".encode() + body
    return "v0=" + hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()


def headers_for(body: bytes, *, at: float = NOW, secret: str = SECRET, **extra):
    timestamp = str(int(at))
    out = {
        "X-Zm-Request-Timestamp": timestamp,
        "X-Zm-Signature": sign(body, timestamp, secret),
        "x-zm-trackingid": "track-1",
    }
    out.update(extra)
    return out


def rtms_body(meeting_uuid: str = "uuid-1") -> bytes:
    return json.dumps(
        {
            "event": "meeting.rtms_started",
            "event_ts": 1_700_000_000_000,
            "payload": {
                "object": {
                    "meeting_uuid": meeting_uuid,
                    "rtms_stream_id": "stream-1",
                    "server_urls": "wss://rtms-signal.zoom.us:443",
                }
            },
        }
    ).encode("utf-8")


class FakeRunner:
    """Records what would have been run, without opening a stream."""

    def __init__(self, *, block: threading.Event | None = None):
        self.handled: list[dict] = []
        self.block = block
        self.slots = threading.Semaphore(1)
        self.reports: dict[str, ZoomReport] = {}

    def handle_rtms_started(self, payload):
        if self.block is not None:
            self.block.wait(5.0)
        self.handled.append(payload)
        report = ZoomReport(meeting_id="uuid-1", spoke_via="null")
        self.reports["uuid-1"] = report
        return report

    def summary_for(self, meeting_id):
        report = self.reports.get(meeting_id)
        return report.summary() if report else NO_RUN_REMEMBERED

    def acquire_slot(self, timeout=0.0):
        return self.slots.acquire(timeout=timeout or 0.01)

    def release_slot(self):
        self.slots.release()


@pytest.fixture
def dispatcher():
    return Dispatcher(CONFIG, FakeRunner(), slot_wait_s=0.5)


# -- verification -----------------------------------------------------------


def test_a_forged_signature_is_refused_and_nothing_is_run(dispatcher):
    body = rtms_body()
    headers = headers_for(body, secret="not-the-secret")
    code, response = dispatcher.handle_request(headers, body, now=NOW)
    dispatcher.join()
    assert code == 401
    assert "error" in response
    assert dispatcher.runner.handled == []


def test_an_unsigned_request_is_refused(dispatcher):
    body = rtms_body()
    code, _ = dispatcher.handle_request({}, body, now=NOW)
    assert code == 401
    assert dispatcher.runner.handled == []


def test_a_stale_delivery_is_refused_as_a_possible_replay(dispatcher):
    body = rtms_body()
    headers = headers_for(body, at=NOW - 3600)
    code, response = dispatcher.handle_request(headers, body, now=NOW)
    assert code == 401
    assert response["reason"] == "stale"


def test_a_body_edited_after_signing_is_refused(dispatcher):
    body = rtms_body()
    headers = headers_for(body)
    code, _ = dispatcher.handle_request(headers, body + b" ", now=NOW)
    assert code == 401
    assert dispatcher.runner.handled == []


# -- the handshake ----------------------------------------------------------


def test_url_validation_is_answered_inline_with_the_hmac(dispatcher):
    plain = "qgg8vlvZRS6UYooatFL8Aw"
    body = json.dumps(
        {"event": "endpoint.url_validation", "payload": {"plainToken": plain}}
    ).encode("utf-8")
    code, response = dispatcher.handle_request(headers_for(body), body, now=NOW)
    expected = hmac.new(
        SECRET.encode("utf-8"), plain.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    assert code == 200
    assert response == {"plainToken": plain, "encryptedToken": expected}
    assert dispatcher.runner.handled == []


def test_a_handshake_with_no_plain_token_is_a_400_not_a_reply(dispatcher):
    body = json.dumps({"event": "endpoint.url_validation", "payload": {}}).encode()
    code, response = dispatcher.handle_request(headers_for(body), body, now=NOW)
    assert code == 400
    assert "plainToken" in response["error"]


# -- dispatch ---------------------------------------------------------------


def test_an_rtms_start_is_acknowledged_before_the_work_happens():
    """Zoom's deadline is seconds; the run is minutes. The 200 promises a run."""
    gate = threading.Event()
    runner = FakeRunner(block=gate)
    dispatcher = Dispatcher(CONFIG, runner, slot_wait_s=1.0)
    body = rtms_body()
    code, response = dispatcher.handle_request(headers_for(body), body, now=NOW)
    assert (code, response["accepted"]) == (200, "uuid-1")
    assert runner.handled == []  # still blocked, and already answered
    gate.set()
    dispatcher.join()
    assert len(runner.handled) == 1


def test_a_replayed_delivery_does_not_cost_a_second_paid_run(dispatcher):
    body = rtms_body()
    headers = headers_for(body)
    first = dispatcher.handle_request(headers, body, now=NOW)
    second = dispatcher.handle_request(headers, body, now=NOW)
    dispatcher.join()
    assert first[0] == 200
    assert second == (200, {"ok": True, "duplicate": True})
    assert len(dispatcher.runner.handled) == 1


def test_a_replay_is_caught_without_the_tracking_header(dispatcher):
    body = rtms_body()
    headers = headers_for(body)
    headers.pop("x-zm-trackingid")
    dispatcher.handle_request(headers, body, now=NOW)
    code, response = dispatcher.handle_request(headers, body, now=NOW)
    dispatcher.join()
    assert (code, response.get("duplicate")) == (200, True)
    assert len(dispatcher.runner.handled) == 1


def test_a_meeting_outside_the_allowlist_is_acknowledged_and_not_run():
    config = Config(
        client_id="c",
        client_secret="s",
        account_id="a",
        webhook_secret=SECRET,
        meeting_allowlist=("uuid-other",),
    )
    dispatcher = Dispatcher(config, FakeRunner(), slot_wait_s=0.2)
    body = rtms_body()
    code, response = dispatcher.handle_request(headers_for(body), body, now=NOW)
    dispatcher.join()
    assert code == 200
    assert response["ignored"] == "not in the meeting allowlist"
    assert dispatcher.runner.handled == []


def test_other_events_are_acknowledged_and_ignored(dispatcher):
    body = json.dumps(
        {
            "event": "meeting.rtms_stopped",
            "event_ts": 1,
            "payload": {"object": {"meeting_uuid": "uuid-1"}},
        }
    ).encode()
    code, response = dispatcher.handle_request(headers_for(body), body, now=NOW)
    dispatcher.join()
    assert code == 200
    assert response["ignored"] == "meeting.rtms_stopped"
    assert dispatcher.runner.handled == []


def test_a_saturated_runner_refuses_rather_than_queueing_forever(dispatcher):
    """The bound is real: a held slot means the second meeting is not started."""
    assert dispatcher.runner.acquire_slot(timeout=1.0)
    body = rtms_body()
    code, _ = dispatcher.handle_request(headers_for(body), body, now=NOW)
    dispatcher.join()
    assert code == 200
    assert dispatcher.runner.handled == []


# -- run memory -------------------------------------------------------------


def test_a_restart_says_it_forgot_rather_than_answering_about_another_board(
    dispatcher,
):
    assert dispatcher.summary_for("uuid-1") == NO_RUN_REMEMBERED
    body = rtms_body()
    dispatcher.handle_request(headers_for(body), body, now=NOW)
    dispatcher.join()
    assert dispatcher.summary_for("uuid-1") != NO_RUN_REMEMBERED


# -- the server -------------------------------------------------------------


@pytest.fixture
def server():
    runner = FakeRunner()
    httpd = make_server(CONFIG, runner, port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd, runner
    httpd.shutdown()
    httpd.server_close()
    thread.join(5.0)


def get(httpd, path):
    url = f"http://127.0.0.1:{httpd.server_port}{path}"
    with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310
        return response.status, json.loads(response.read())


def post(httpd, path, body, headers):
    url = f"http://127.0.0.1:{httpd.server_port}{path}"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_healthz_names_this_service(server):
    httpd, _ = server
    assert get(httpd, "/healthz") == (200, {"ok": True, "service": "silkscreen-zoom"})


def test_an_unknown_route_is_a_404(server):
    httpd, _ = server
    with pytest.raises(urllib.error.HTTPError) as caught:
        get(httpd, "/zoom/events")
    assert caught.value.code == 404


def test_a_signed_delivery_reaches_the_runner_over_real_http(server):
    httpd, runner = server
    body = rtms_body()
    # The live server has no injected clock, so the signature must be current.
    headers = headers_for(body, at=time.time())
    status, response = post(httpd, "/zoom/events", body, headers)
    assert status == 200
    assert response["accepted"] == "uuid-1"
    for _ in range(100):
        if runner.handled:
            break
        threading.Event().wait(0.02)
    assert len(runner.handled) == 1


def test_a_forged_delivery_is_a_401_over_real_http(server):
    httpd, runner = server
    body = rtms_body()
    headers = headers_for(body, at=time.time(), secret="wrong")
    status, _ = post(httpd, "/zoom/events", body, headers)
    assert status == 401
    assert runner.handled == []


def test_the_webhook_reaches_the_engine_only_through_the_gated_entry_point():
    """The sibling check for ``teamsbot``'s production-path bypass.

    ``teamsbot/app.py``'s default runner used to call ``runner.run_meeting``
    directly, stepping past the refusals and gates in
    ``runner.handle_incoming``. The two packages were written as siblings, so
    the same shape was checked for here: it is not present, and this pins it.

    ``zoombot/app.py`` has exactly one call into the engine half --
    ``ZoomRunner.handle_rtms_started`` -- and every gate (the meeting
    allowlist, the quote gate in ``requests_from_chunks``, the confidence
    floor and ``max_runs_per_meeting``) lives inside ``run_meeting``, which
    only that method calls. There is also no follow-up command surface at all:
    the webhook acts on ``meeting.rtms_started`` and acknowledges everything
    else, so no word said in a meeting reaches a model through this door.
    """
    import inspect

    from zoombot import app as app_module

    source = inspect.getsource(app_module)
    assert "run_meeting" not in source
    assert source.count("handle_rtms_started") == 1
    # ``meetings.intent`` is the quote gate; nothing here may reach around it.
    assert "requests_from_chunks" not in source
    assert "generate_pcb" not in source


def test_a_replay_with_a_fresh_tracking_id_is_still_caught(dispatcher):
    """The replay key must come from the signed body, not from a header.

    ``x-zm-trackingid`` is not covered by the HMAC (``_signature`` signs only
    ``v0:<timestamp>:<body>``), so anyone holding one captured delivery could
    resend the identical signed bytes inside the 300-second window with that
    header varied and have every one accepted. Each acceptance of a
    ``meeting.rtms_started`` is another paid pipeline run.
    """
    body = rtms_body()
    headers = headers_for(body)
    dispatcher.handle_request(headers, body, now=NOW)
    replay = dict(headers)
    replay["x-zm-trackingid"] = "track-2-attacker-chosen"
    code, response = dispatcher.handle_request(replay, body, now=NOW)
    dispatcher.join()
    assert (code, response.get("duplicate")) == (200, True)
    assert len(dispatcher.runner.handled) == 1

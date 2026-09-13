"""The HTTP surface: signatures, acknowledgement, routing, and deduplication.

Named test_http rather than test_app because ``scripts/check_docs.py`` keys its
per-module test counts by file *basename*, and a second ``test_app.py`` would be
silently added to the service's count.
"""

from __future__ import annotations

import io
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from slackbot.app import Dispatcher, make_handler, make_server
from slackbot.config import Config
from slackbot.tests.test_slack import SECRET, sign

CONFIG = Config(bot_token="xoxb-t", signing_secret=SECRET)


class FakeRunner:
    """Records what would have been executed, without executing anything."""

    def __init__(self):
        self.handled: list[tuple] = []
        self.errors: list[str] = []
        self.busy = 0
        self.slots = threading.Semaphore(2)

    def handle(self, command, *, channel, thread_ts, user=""):
        self.handled.append((command, channel, thread_ts, user))

    def report_error(self, channel, thread_ts, message):
        self.errors.append(message)

    def report_busy(self, channel, thread_ts):
        self.busy += 1

    def acquire_slot(self, timeout=0.0):
        return self.slots.acquire(timeout=timeout or 0.01)

    def release_slot(self):
        self.slots.release()


@pytest.fixture
def dispatcher():
    # A short slot wait: the production 240s is there to absorb one run ahead
    # in the queue, and waiting it out would make this file take four minutes.
    return Dispatcher(CONFIG, FakeRunner(), slot_wait_s=0.05)


def event(text="design a rail", **overrides):
    payload = {
        "type": "event_callback",
        "event_id": "Ev1",
        "event": {
            "type": "app_mention",
            "text": f"<@U0BOT> {text}",
            "channel": "C1",
            "ts": "1.1",
            "user": "U9",
        },
    }
    payload["event"].update(overrides.pop("event", {}))
    payload.update(overrides)
    return payload


# -- dispatcher -----------------------------------------------------------


def test_url_verification_echoes_the_challenge():
    code, body = Dispatcher(CONFIG, FakeRunner()).handle_event_payload(
        {"type": "url_verification", "challenge": "abc123"}
    )
    assert (code, body) == (200, {"challenge": "abc123"})


def test_a_mention_schedules_a_run(dispatcher):
    code, _ = dispatcher.handle_event_payload(event())
    dispatcher.join()
    assert code == 200
    command, channel, thread_ts, user = dispatcher.runner.handled[0]
    assert command.verb == "design"
    assert (channel, thread_ts, user) == ("C1", "1.1", "U9")


def test_a_mention_inside_a_thread_answers_in_that_thread(dispatcher):
    """`review` and `order` refer to the run above them."""
    dispatcher.handle_event_payload(event("review", event={"thread_ts": "0.9"}))
    dispatcher.join()
    assert dispatcher.runner.handled[0][2] == "0.9"


def test_a_repeated_delivery_runs_once(dispatcher):
    """Slack retries an unacknowledged event; a second run costs real money."""
    dispatcher.handle_event_payload(event())
    dispatcher.handle_event_payload(event())
    dispatcher.join()
    assert len(dispatcher.runner.handled) == 1


def test_a_retry_with_a_fresh_event_id_still_runs_once(dispatcher):
    """The spend guard must not rest on an assumption nobody published.

    Slack's Events API docs state the three-second deadline, the three retries
    and the ``x-slack-retry-num`` header. They do **not** state that a retried
    delivery carries the same ``event_id``. So this drives the case the docs
    leave open -- retry number 2, same message, a different ``event_id`` -- and
    requires one run, which only the derived key (team/type/channel/``ts``, the
    message's own identity) can deliver.
    """
    dispatcher.handle_event_payload(event())
    code, body = dispatcher.handle_event_payload(
        event(event_id="Ev-different"), retry_num=2
    )
    dispatcher.join()
    assert len(dispatcher.runner.handled) == 1
    assert code == 200
    assert body == {"ok": True}


def test_two_different_messages_in_one_channel_both_run(dispatcher):
    """The derived key is an identity, not a category.

    The zoombot lane's defect was a key that hashed every event the same, so
    the first was accepted and every later one refused as a duplicate. Two
    mentions in one channel differ only by ``ts``, which is exactly the field
    that makes the key an identity, so both must run.
    """
    dispatcher.handle_event_payload(event(event_id="Ev1"))
    dispatcher.handle_event_payload(
        event(event_id="Ev2", event={"ts": "2.2", "thread_ts": "2.2"})
    )
    dispatcher.join()
    assert len(dispatcher.runner.handled) == 2


def test_a_duplicate_asks_slack_to_stop_retrying(server):
    """Slack: "provide us this HTTP header and value: x-slack-no-retry: 1"."""
    httpd, dispatcher = server
    body = json.dumps(event()).encode()
    first = post_with_headers(httpd, "/slack/events", body)
    second = post_with_headers(httpd, "/slack/events", body)
    dispatcher.join()
    assert first[0] == second[0] == 200
    # Only the duplicate carries it: a first delivery that failed for real
    # still wants the retry.
    assert first[2].get("x-slack-no-retry") is None
    assert second[2].get("x-slack-no-retry") == "1"
    assert len(dispatcher.runner.handled) == 1


def test_the_bot_ignores_other_bots(dispatcher):
    dispatcher.handle_event_payload(event(event={"bot_id": "B1"}))
    dispatcher.join()
    assert dispatcher.runner.handled == []


def test_non_mention_events_are_ignored(dispatcher):
    dispatcher.handle_event_payload(event(event={"type": "message"}))
    dispatcher.join()
    assert dispatcher.runner.handled == []


def test_a_disallowed_channel_is_ignored():
    config = Config(
        bot_token="t", signing_secret=SECRET, allowed_channels=frozenset({"C-ok"})
    )
    dispatcher = Dispatcher(config, FakeRunner(), slot_wait_s=0.05)
    dispatcher.handle_event_payload(event())
    dispatcher.join()
    assert dispatcher.runner.handled == []


def test_a_parse_error_is_relayed_rather_than_run(dispatcher):
    dispatcher.handle_event_payload(event("design a rail --datasheet"))
    dispatcher.join()
    assert dispatcher.runner.handled == []
    assert "PART=URL" in dispatcher.runner.errors[0]


def test_saturation_is_reported(dispatcher):
    dispatcher.runner.slots = threading.Semaphore(0)
    dispatcher.handle_event_payload(event())
    dispatcher.join()
    assert dispatcher.runner.busy == 1
    assert dispatcher.runner.handled == []


def test_cheap_verbs_do_not_consume_a_slot(dispatcher):
    dispatcher.runner.slots = threading.Semaphore(0)
    dispatcher.handle_event_payload(event("help"))
    dispatcher.join()
    assert dispatcher.runner.handled[0][0].verb == "help"
    assert dispatcher.runner.busy == 0


def test_a_slash_command_posts_into_the_channel_not_a_dm(dispatcher):
    code, body = dispatcher.handle_slash_payload(
        {"channel_id": "C1", "user_id": "U9", "text": "design a rail"}
    )
    dispatcher.join()
    assert code == 200
    assert body["response_type"] == "ephemeral"
    assert dispatcher.runner.handled[0][1] == "C1"


# -- HTTP -----------------------------------------------------------------


@pytest.fixture
def server(dispatcher):
    from http.server import ThreadingHTTPServer

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(dispatcher))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd, dispatcher
    httpd.shutdown()
    httpd.server_close()


def post(httpd, path, body: bytes, *, signed=True, timestamp=None):
    # The default has to be *now*: the server checks freshness against the real
    # clock, so a fixed timestamp would make these tests fail with age.
    timestamp = timestamp or str(int(time.time()))
    headers = {"Content-Type": "application/json"}
    if signed:
        headers["X-Slack-Request-Timestamp"] = timestamp
        headers["X-Slack-Signature"] = sign(body, timestamp)
    request = urllib.request.Request(
        f"http://127.0.0.1:{httpd.server_port}{path}",
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def post_with_headers(httpd, path, body: bytes, *, retry_num: int | None = None):
    """Like :func:`post`, but hands back the response headers too."""
    timestamp = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": sign(body, timestamp),
    }
    if retry_num is not None:
        headers["X-Slack-Retry-Num"] = str(retry_num)
        headers["X-Slack-Retry-Reason"] = "http_timeout"
    request = urllib.request.Request(
        f"http://127.0.0.1:{httpd.server_port}{path}",
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read(), _lower(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), _lower(exc.headers)


def _lower(headers) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items()}


def test_healthz(server):
    httpd, _ = server
    with urllib.request.urlopen(
        f"http://127.0.0.1:{httpd.server_port}/healthz", timeout=5
    ) as response:
        assert json.loads(response.read())["ok"] is True


def test_an_unsigned_request_is_rejected_without_being_parsed(server):
    httpd, dispatcher = server
    status, _ = post(httpd, "/slack/events", json.dumps(event()).encode(), signed=False)
    assert status == 401
    dispatcher.join(timeout=1)
    assert dispatcher.runner.handled == []


def test_a_stale_signature_is_rejected(server):
    httpd, dispatcher = server
    status, _ = post(
        httpd, "/slack/events", json.dumps(event()).encode(), timestamp="1"
    )
    assert status == 401
    assert dispatcher.runner.handled == []


def test_a_signed_event_is_acknowledged(server):
    httpd, dispatcher = server
    status, body = post(httpd, "/slack/events", json.dumps(event()).encode())
    assert status == 200
    dispatcher.join()
    assert dispatcher.runner.handled[0][0].verb == "design"


def test_url_verification_over_http(server):
    httpd, _ = server
    payload = json.dumps({"type": "url_verification", "challenge": "xyz"}).encode()
    status, body = post(httpd, "/slack/events", payload)
    assert status == 200
    assert json.loads(body)["challenge"] == "xyz"


def test_a_signed_but_non_json_body_is_a_400(server):
    httpd, _ = server
    assert post(httpd, "/slack/events", b"not json")[0] == 400


def test_a_slash_command_form_body_is_parsed(server):
    httpd, dispatcher = server
    body = b"channel_id=C1&user_id=U9&text=design+a+rail"
    status, _ = post(httpd, "/slack/commands", body)
    assert status == 200
    dispatcher.join()
    assert dispatcher.runner.handled[0][0].intent == "a rail"


def test_unknown_routes_are_404(server):
    httpd, _ = server
    assert post(httpd, "/slack/nope", b"{}")[0] == 404


def test_an_oversized_body_is_refused(server):
    httpd, _ = server
    assert post(httpd, "/slack/events", b"x" * (2 << 20))[0] == 413


def test_make_server_binds(monkeypatch):
    httpd = make_server(Config(bot_token="t", signing_secret="s", port=0))
    try:
        assert httpd.server_port > 0
    finally:
        httpd.server_close()


class _CountingReader(io.RawIOBase):
    """An endless body, counting what the server actually consumed."""

    def __init__(self):
        self.consumed = 0

    def read(self, size=-1):
        size = 64 << 10 if size is None or size < 0 else size
        self.consumed += size
        return b"x" * size

    def readable(self):
        return True


def test_an_oversized_body_is_not_read_in_full():
    """Greptile P1 on PR #13.

    Draining a declared body before authenticating it let an unauthenticated
    client hold a handler thread for as long as it cared to send bytes, on a
    thread-per-connection server. The read is bounded now, so a body declared
    as 64 MB costs a bounded prefix and then the connection.
    """
    from slackbot.app import MAX_BODY_BYTES, MAX_DRAIN_BYTES

    handler = make_handler(Dispatcher(CONFIG, FakeRunner())).__new__(
        make_handler(Dispatcher(CONFIG, FakeRunner()))
    )
    reader = _CountingReader()
    handler.rfile = reader
    handler.headers = {"Content-Length": str(64 << 20)}
    handler.close_connection = False
    sent: list[int] = []
    handler._send = lambda code, payload="", content_type="": sent.append(code)

    assert handler._read_body() is None
    assert sent == [413]
    assert handler.close_connection is True
    assert reader.consumed <= MAX_DRAIN_BYTES
    # Bounded, and still generous enough that a client which merely overshot
    # the limit reads its 413 instead of seeing a reset socket.
    assert MAX_DRAIN_BYTES >= MAX_BODY_BYTES


def test_the_handler_sets_a_socket_timeout():
    """A slow client must not be able to hold a thread indefinitely."""
    from slackbot.app import SOCKET_TIMEOUT_S

    handler = make_handler(Dispatcher(CONFIG, FakeRunner()))
    assert handler.timeout == SOCKET_TIMEOUT_S


def test_a_dribbled_oversized_body_is_abandoned_on_a_deadline(monkeypatch):
    """Greptile P1 on PR #13, second round.

    Bounding the drained *bytes* left the drained *time* unbounded: the socket
    timeout restarts on every read, so an unauthenticated client sending one
    byte per window could hold a handler thread for hours while staying inside
    MAX_DRAIN_BYTES. The drain now runs against a wall-clock deadline and
    abandons the connection past it, without ever owing the client its 413.
    """
    from slackbot import app as A
    from slackbot.app import Dispatcher as D

    class _Clock:
        def __init__(self):
            self.now = 0.0

        def monotonic(self):
            self.now += 10.0  # every look at the clock costs ten seconds
            return self.now

    monkeypatch.setattr(A, "time", _Clock())

    class _Dribbler(io.RawIOBase):
        """One byte per read, forever — the slow-loris body."""

        def __init__(self):
            self.consumed = 0

        def read(self, size=-1):
            self.consumed += 1
            return b"x"

        def readable(self):
            return True

    handler_cls = make_handler(D(CONFIG, FakeRunner()))
    handler = handler_cls.__new__(handler_cls)
    reader = _Dribbler()
    handler.rfile = reader
    handler.headers = {"Content-Length": str(64 << 20)}
    handler.close_connection = False
    sent: list[int] = []
    handler._send = lambda code, payload="", content_type="": sent.append(code)

    assert handler._read_body() is None
    assert handler.close_connection is True
    # Abandoned mid-drain: a client that dribbled past the deadline was never
    # going to read a 413, so none is sent.
    assert sent == []
    # A handful of reads, not two megabytes of one-byte reads.
    assert reader.consumed < 16

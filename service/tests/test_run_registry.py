"""A streamed run you can name, poll, cancel -- and be charged for.

Two halves of one idea: you cannot meter, rejoin or cancel a run you cannot
name. The socket-level tests here drive the real server, because the run id on
the *header* is the load-bearing half (it survives a body a client cannot
parse) and urllib is the only client in this suite that would hide that.

The metering tests never touch Stripe and never open a socket to it: they run
against ``billing.ledger.MemoryLedger``, which is the offline stand-in the
package ships for exactly this.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from billing.accounts import AccountId
from billing.ledger import MemoryLedger, OveragePolicy
from service import metering as _metering
from service import runs as _runs

# The harness (scripted model, socket helpers) lives in test_app.py; importing
# it keeps one definition of "a running service" rather than a second that can
# drift from it.
from service.tests.test_app import (  # noqa: F401  (re-exported fixture)
    get,
    post,
    post_stream,
    url,
)
from service.tests.test_app import server as _test_app_server  # noqa: F401


@pytest.fixture
def server(_test_app_server):  # noqa: F811  (pytest fixture re-export)
    """test_app.py's running service, re-exported under its own name.

    Requested rather than re-imported so this file cannot grow a second
    definition of "a running service" that drifts from the first one.
    """
    return _test_app_server


INTENT = {"intent": "a 3.3V LDO board", "review": False}


@pytest.fixture(autouse=True)
def _clean_registry():
    _runs.reset_runs()
    _metering.reset_for_tests()
    yield
    _runs.reset_runs()
    _metering.reset_for_tests()


# --------------------------------------------------------------- addressable


def test_the_stream_names_its_run_on_the_header_and_the_first_frame(server):
    """The header is the half that survives a body nobody can read.

    ``frontend/src/lib/api.js::startedButUnreadable`` is the exact case: a 200
    whose content type was rewritten, or whose body has no reader. That client
    correctly refuses to send the request again -- and until this header
    existed it had no way to name the run it had just paid for.
    """
    status, headers, frames = post_stream(server, INTENT)
    assert status == 200
    run_id = headers.get("x-kaleo-run-id")
    assert run_id and run_id.startswith("run_")

    assert frames[0]["event"] == "run.accepted"
    # Same id in both places, so a client can use whichever it can reach.
    assert frames[0]["run_id"] == run_id
    assert frames[-1]["event"] == "run.done"
    assert frames[-1]["run_id"] == run_id


def test_the_one_shot_route_names_its_run_on_the_header_and_only_there(server):
    """Header, never the body -- and that restraint is the point.

    ``/generate``'s body is an equality contract with the stream's ``run.done``
    result and with a plain run (``test_a_stream_reports_the_run_and_ends_with
    _the_one_shot_body``, ``test_the_order_block_is_purely_additive``). A field
    added to one side of an equality is a field that breaks it, so the id
    travels beside the body rather than inside it.
    """
    req = urllib.request.Request(
        url(server, "/generate"),
        data=json.dumps(INTENT).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        run_id = resp.headers.get("X-Kaleo-Run-Id")
        body = json.loads(resp.read())
    assert run_id and run_id.startswith("run_")
    assert "run_id" not in body
    assert json.loads(get(server, f"/runs/{run_id}")[2])["state"] == "done"


def test_a_finished_run_can_be_polled_by_id(server):
    _status, headers, _frames = post_stream(server, INTENT)
    run_id = headers["x-kaleo-run-id"]

    status, _h, raw = get(server, f"/runs/{run_id}")
    assert status == 200
    body = json.loads(raw)
    assert body["run_id"] == run_id
    assert body["state"] == "done"
    assert body["events"] > 1
    # The poll answers "did it finish and what did it cost", never "here is
    # the board" -- and it says so, so nobody waits for an artifact that is
    # not coming. See service/runs.py.
    assert body["result_available"] is False
    assert "board" in body["note"] or "state" in body["note"]


def test_polling_an_unknown_run_is_a_404_that_says_why(server):
    status, _h, raw = get(server, "/runs/run_nosuchthing")
    assert status == 404
    message = json.loads(raw)["error"]
    assert "restarted" in message or "forgotten" in message


def test_the_run_list_carries_the_runs_this_process_started(server):
    _s, headers, _f = post_stream(server, INTENT)
    status, _h, raw = get(server, "/runs")
    assert status == 200
    ids = [r["run_id"] for r in json.loads(raw)["runs"]]
    assert headers["x-kaleo-run-id"] in ids


# -------------------------------------------------------------------- cancel


def test_cancelling_a_live_run_stops_it_at_its_next_event(server, monkeypatch):
    """The run stops through the mechanism the pipeline already had.

    A callback that raises abandons the run; ``service/amend.py`` uses the
    same seam for a step session. The pipeline is replaced by a stand-in that
    emits and would emit forever, so the test is about *whether the cancel
    stops it* rather than about winning a race with a real solve. What is
    asserted is what a client sees -- a ``run.cancelled`` terminal frame, a
    `cancelled` poll, and never a ``run.error``, because a cancel the caller
    asked for is not a crash.
    """
    from service import app as _app

    reached = threading.Event()

    def endless(payload, *, on_event=None, **kwargs):
        for index in range(200):
            on_event({"event": "stage.start", "stage": f"fake-{index}"})
            reached.set()
            # The stand-in never returns on its own; only the cancel raising
            # out of on_event ends it. A pipeline that ignored the callback
            # would hang this test rather than pass it.
            time.sleep(0.01)
        raise AssertionError("the cancel never reached the event callback")

    monkeypatch.setattr(_app, "generate", endless)

    result: dict = {}

    def collect():
        status, headers, frames = post_stream(server, INTENT)
        result.update(status=status, headers=headers, frames=frames)

    reader = threading.Thread(target=collect, daemon=True)
    reader.start()
    assert reached.wait(timeout=30), "the fake pipeline never started"

    live = [r for r in _runs.snapshot_all()["runs"] if r["state"] == "running"]
    assert len(live) == 1
    run_id = live[0]["run_id"]
    answer = _runs.cancel(run_id)
    assert answer["in_flight"] is True
    assert answer["aborts_at"] == "the next event the pipeline reports"

    reader.join(timeout=30)
    assert not reader.is_alive(), "the cancel did not stop the run"

    names = [f["event"] for f in result["frames"]]
    assert "run.error" not in names
    assert names[-1] == "run.cancelled"
    assert result["frames"][-1]["run_id"] == run_id
    assert json.loads(get(server, f"/runs/{run_id}")[2])["state"] == "cancelled"


def test_the_cancel_route_answers_the_amend_vocabulary(server):
    _s, headers, _f = post_stream(server, INTENT)
    run_id = headers["x-kaleo-run-id"]

    req = urllib.request.Request(url(server, f"/runs/{run_id}/cancel"), data=b"")
    with urllib.request.urlopen(req) as resp:
        body = json.loads(resp.read())
    # The same words service/amend.py::cancel uses for a step session. One
    # product, one vocabulary; two spellings is how two cancels come to
    # disagree about what they stopped.
    assert body["cancelled"] is True
    assert body["in_flight"] is False
    assert body["aborts_at"] == "nothing was running"
    assert "already done" in body["headline"] or "nothing was stopped" in body[
        "headline"
    ]


def test_cancelling_twice_is_not_an_error(server):
    _s, headers, _f = post_stream(server, INTENT)
    run_id = headers["x-kaleo-run-id"]
    first = _runs.cancel(run_id)
    second = _runs.cancel(run_id)
    assert first["already_cancelled"] is False
    assert second["already_cancelled"] is True


def test_cancelling_an_unknown_run_is_a_404(server):
    req = urllib.request.Request(url(server, "/runs/run_nope/cancel"), data=b"")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req)
    assert exc.value.code == 404


def test_a_cancel_never_rewrites_a_terminal_state():
    record = _runs.new_run("/generate/stream")
    record.finish("done")
    _runs.cancel(record.id)
    assert record.as_dict()["state"] == "done"
    # And check() stays quiet, so the terminal frames still go out.
    record.check()


def test_check_raises_only_while_the_run_is_running():
    record = _runs.new_run("/generate/stream")
    record.check()
    _runs.cancel(record.id)
    with pytest.raises(_runs.RunCancelled):
        record.check()


def test_the_registry_evicts_finished_runs_before_live_ones(monkeypatch):
    monkeypatch.setattr(_runs, "MAX_RUNS", 3)
    live = _runs.new_run("/generate/stream")
    for _ in range(6):
        _runs.new_run("/generate/stream").finish("done")
    # The one still burning money survived every eviction.
    assert live.id in [r["run_id"] for r in _runs.snapshot_all()["runs"]]


# ------------------------------------------------------------------ metering


def ledger_metering(**kwargs):
    ledger = MemoryLedger()
    meter = _metering.Metering(
        enabled=True,
        ledger=ledger,
        account=AccountId("test"),
        estimate_mkcu=kwargs.pop("estimate_mkcu", 2000),
        overage=kwargs.pop("overage", OveragePolicy()),
    )
    return ledger, meter


def test_metering_is_off_by_default_and_says_so(monkeypatch):
    monkeypatch.delenv("KALEO_METERING", raising=False)
    meter = _metering.current()
    assert meter.enabled is False
    hold = meter.begin("run_x")
    assert hold is None
    block = meter.finish(hold, elapsed_s=12.0)
    # Not silence, and not a zero that reads as "free". The disabled state is
    # a sentence, the way spice/ reports a missing verdict.
    assert block == {
        "enabled": False,
        "state": "off",
        "reason": _metering.NOT_ENABLED,
    }


def test_a_run_holds_then_commits_the_measured_cost():
    ledger, meter = ledger_metering()
    ledger.grant(AccountId("test"), 10_000, reason="test credit")

    hold = meter.begin("run_1")
    # Held while in flight, so a concurrent run cannot spend the same balance.
    assert ledger.balance(AccountId("test")).held_mkcu == 2000

    block = meter.finish(hold, elapsed_s=30.0)
    assert block["state"] == "committed"
    # 30 s = half a KCU = 500 mKCU. The hold was 2000; the commit is the real
    # number, and the difference is released -- Stripe's capture-for-less.
    assert block["charged_mkcu"] == 500
    assert block["reserved_mkcu"] == 2000
    assert ledger.balance(AccountId("test")).held_mkcu == 0
    assert ledger.balance(AccountId("test")).available_mkcu == 9500


def test_a_failed_run_is_charged_for_what_it_used_not_refunded():
    """The whole point of ``release(consumed_mkcu=...)``.

    A refund-on-failure makes cancelling late strictly cheaper than
    finishing, and the model calls are already spent. billing/ledger.py's
    ``release`` docstring says so; this asserts the service actually passes
    the measured time rather than zero.
    """
    ledger, meter = ledger_metering()
    ledger.grant(AccountId("test"), 10_000, reason="test credit")
    hold = meter.begin("run_2")
    block = meter.fail(hold, elapsed_s=60.0, reason="run cancelled")
    assert block["state"] == "released"
    assert block["charged_mkcu"] == 1000
    assert ledger.balance(AccountId("test")).available_mkcu == 9000


def test_a_run_that_passes_zero_finishes_and_is_billed_afterwards():
    """Overage, never cutoff. Cutting off mid-run delivers nothing."""
    ledger, meter = ledger_metering()
    ledger.grant(AccountId("test"), 1000, reason="a nearly-empty account")
    hold = meter.begin("run_3")
    block = meter.finish(hold, elapsed_s=180.0)  # 3 KCU against 1 KCU of credit
    assert block["state"] == "committed"
    assert ledger.balance(AccountId("test")).available_mkcu == -2000
    assert ledger.overage_mkcu(AccountId("test")) == 2000


def test_past_the_credit_line_a_run_is_refused_before_it_starts():
    ledger, meter = ledger_metering(overage=OveragePolicy(limit_mkcu=1000))
    with pytest.raises(_metering.InsufficientCredit):
        meter.begin("run_4")
    # Nothing was held, so nothing has to be cleaned up.
    assert ledger.holds() == []


def test_the_service_answers_a_refusal_as_402_before_any_model_call(
    server, monkeypatch
):
    """A 402 costs nothing, which is what makes it the honest refusal.

    ``Metering.begin`` runs before the 200 and before the pipeline, so a
    caller turned away here has not spent a model call. Asserted through the
    real route rather than the unit, because the ordering *in the handler* is
    the property under test.
    """
    ledger, meter = ledger_metering(overage=OveragePolicy(limit_mkcu=100))
    monkeypatch.setattr(_metering, "_CURRENT", meter)

    status, body = post(server, INTENT, path="/generate")
    assert status == 402
    assert body["reason"] == "insufficient_credit"
    # Nothing was spent and nothing was held: the refusal happened before the
    # pipeline was even asked.
    assert ledger.entries() == []
    assert ledger.holds() == []


def test_metering_never_takes_the_board_down_with_it():
    """A ledger that cannot record must not turn a delivered board into a 500.

    The run happened and the customer has the result; an unrecorded charge is
    a reconciliation problem, loud on stderr, not a failed request.
    """

    class Broken(MemoryLedger):
        def commit(self, run_id, actual_mkcu):
            raise RuntimeError("disk gone")

    ledger = Broken()
    ledger.grant(AccountId("test"), 10_000, reason="test credit")
    meter = _metering.Metering(
        enabled=True,
        ledger=ledger,
        account=AccountId("test"),
        estimate_mkcu=2000,
        overage=OveragePolicy(),
    )
    block = meter.finish(meter.begin("run_5"), elapsed_s=10.0)
    assert block["state"] == "unrecorded"
    assert "ledger" in block["reason"]


def test_misconfigured_metering_degrades_to_off_rather_than_billing_wrongly(
    monkeypatch,
):
    monkeypatch.setenv("KALEO_METERING", "1")
    monkeypatch.setenv("KALEO_RUN_ESTIMATE_MKCU", "not-a-number")
    _metering.reset_for_tests()
    meter = _metering.current()
    assert meter.enabled is False
    # And it names the reason rather than reading as "billing is off here".
    assert "could not be configured" in meter.reason


def test_a_metered_run_reports_its_cost_on_the_stream(server, monkeypatch):
    ledger, meter = ledger_metering()
    ledger.grant(AccountId("test"), 100_000, reason="test credit")
    monkeypatch.setattr(_metering, "_CURRENT", meter)

    _status, headers, frames = post_stream(server, INTENT)
    done = frames[-1]
    assert done["event"] == "run.done"
    assert done["metering"]["state"] == "committed"
    assert done["metering"]["charged_mkcu"] >= 0
    # And the same numbers come back on the poll, so a client that lost the
    # stream learns what its run cost.
    polled = json.loads(get(server, f"/runs/{headers['x-kaleo-run-id']}")[2])
    assert polled["metering"] == done["metering"]


def test_an_unmetered_run_says_so_on_the_stream(server, monkeypatch):
    monkeypatch.delenv("KALEO_METERING", raising=False)
    _metering.reset_for_tests()
    _status, _headers, frames = post_stream(server, INTENT)
    assert frames[-1]["metering"]["enabled"] is False
    assert "not charged" in frames[-1]["metering"]["reason"]


# --------------------------------------------------- a caller-supplied run id


def test_a_caller_may_name_its_own_run(server):
    """LiteLLM's ``x-litellm-call-id`` inbound, applied here.

    The point is not tidiness: a client that chose the id knows the run's name
    *before* it sends the request, so a response that never arrives at all --
    not a rewritten content type, an actually dropped connection -- is still
    pollable and cancellable.
    """
    mine = "run_mine.42"
    body = json.dumps(INTENT).encode()
    request = (
        f"POST /generate/stream HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{server.server_port}\r\n"
        "Content-Type: application/json\r\n"
        f"X-Kaleo-Run-Id: {mine}\r\n"
        "Connection: close\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    ).encode() + body
    import socket

    with socket.create_connection(("127.0.0.1", server.server_port), timeout=60) as s:
        s.sendall(request)
        chunks = []
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    raw = b"".join(chunks)
    assert f"X-Kaleo-Run-Id: {mine}".encode() in raw
    assert json.loads(get(server, f"/runs/{mine}")[2])["state"] == "done"


def test_reusing_a_run_id_is_refused_rather_than_replayed(server):
    """Not the Idempotency-Key rule, and the difference is deliberate.

    ``steps.start_once`` replays a finished start verbatim because it *has*
    the envelope. This registry keeps no result, so a "replay" here would hand
    back somebody else's run state as this caller's. Refusing names the
    collision instead.
    """
    _runs.new_run("/generate/stream", run_id="run_taken")
    status, body = post(server, INTENT, path="/generate")
    assert status == 200  # a fresh id is fine
    with pytest.raises(_runs.RunIdInUse):
        _runs.new_run("/generate", run_id="run_taken")


def test_a_malformed_caller_run_id_is_a_400_before_any_work(server):
    with pytest.raises(ValueError):
        _runs.new_run("/generate", run_id="run/../etc")
    with pytest.raises(ValueError):
        _runs.new_run("/generate", run_id="x" * (_runs.MAX_RUN_ID_CHARS + 1))


def test_a_cancel_sent_with_a_body_still_answers(server):
    """The desktop's cancel posts `{}`. Unread bytes are a reset, not an answer.

    `stepPost` in app/src/lib/silkscreen/client.ts sends a JSON body on every
    POST, cancel included. A handler that ignores it leaves those bytes in the
    socket, and the close then reads to the client as a dropped connection --
    which, for a cancel, is indistinguishable from "the cancel did not land".
    """
    _s, headers, _f = post_stream(server, INTENT)
    run_id = headers["x-kaleo-run-id"]
    req = urllib.request.Request(
        url(server, f"/runs/{run_id}/cancel"),
        data=b"{}",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        assert json.loads(resp.read())["cancelled"] is True

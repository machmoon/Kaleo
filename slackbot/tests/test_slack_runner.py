"""Running commands end to end, with the pipeline and Slack both stubbed.

``generate_pcb`` is monkeypatched rather than called: the real one needs an API
key and a CP-SAT solve, and what is under test here is what the bot does with a
result, not that the engine produces one. The engine has its own suite for that.
"""

from __future__ import annotations

import json

import pytest
from silkscreen.agents.model import ModelError
from silkscreen.agents.review import Severity

from slackbot import runner as R
from slackbot.commands import Command, parse_command
from slackbot.config import Config
from slackbot.slack import SlackClient
from slackbot.tests.fakes import RecordingTransport, fake_result, finding

CONFIG_KWARGS = {"bot_token": "xoxb-t", "signing_secret": "s"}


@pytest.fixture
def setup(tmp_path):
    transport = RecordingTransport(
        {
            "getUploadURLExternal": {
                "ok": True,
                "upload_url": "https://files.slack.test/u",
                "file_id": "F1",
            }
        }
    )
    config = Config(**CONFIG_KWARGS, workdir=tmp_path / "runs")
    client = SlackClient("xoxb-t", transport=transport)
    runner = R.Runner(config, client, model_factory=lambda: object())
    return transport, runner


def posted(transport) -> str:
    return json.dumps(transport.calls("chat.postMessage"), ensure_ascii=False)


def install_pipeline(monkeypatch, result, *, board_text: str = "(kicad_pcb v1)"):
    """Stub generate_pcb, writing a board file the way the real one does."""
    seen: dict = {}

    def fake_generate(model, intent, **kwargs):
        seen.update(kwargs, intent=intent)
        output = kwargs.get("output")
        if output:
            output.write_text(board_text, encoding="utf-8")
            result.board_path = output
        on_event = kwargs.get("on_event")
        if on_event:
            on_event({"event": "stage.start", "stage": "propose"})
            on_event({"event": "stage.done", "stage": "propose"})
            on_event({"event": "stage.start", "stage": "place"})
            on_event({"event": "stage.done", "stage": "place"})
        return result

    monkeypatch.setattr(R, "generate_pcb", fake_generate)
    return seen


def test_help_posts_the_help_text(setup):
    transport, runner = setup
    runner.handle(Command(verb="help"), channel="C1", thread_ts="1.1")
    assert "@silkscreen design" in posted(transport)


def test_a_design_run_posts_progress_result_image_and_board(setup, monkeypatch):
    transport, runner = setup
    install_pipeline(monkeypatch, fake_result())

    runner.handle(
        parse_command("design a 3v3 rail"), channel="C1", thread_ts="1.1", user="U9"
    )

    messages = posted(transport)
    assert "Working on it" in messages
    assert "Board ready" in messages
    # The preview and the board file are two separate uploads.
    completes = transport.calls("files.completeUploadExternal")
    titles = [c["files"][0]["title"] for c in completes]
    assert any(t.endswith((".png", ".svg")) for t in titles)
    assert any(t.endswith(".kicad_pcb") for t in titles)


def test_everything_is_threaded_under_the_triggering_message(setup, monkeypatch):
    """A hardware team reads the channel later; nothing may go to a DM."""
    transport, runner = setup
    install_pipeline(monkeypatch, fake_result())
    runner.handle(parse_command("design a 3v3 rail"), channel="C1", thread_ts="1.1")

    for call in transport.calls("chat.postMessage"):
        assert call["thread_ts"] == "1.1"
        assert call["channel"] == "C1"
    for call in transport.calls("files.completeUploadExternal"):
        assert call["thread_ts"] == "1.1"


def test_config_budgets_reach_the_pipeline(setup, monkeypatch):
    transport, runner = setup
    seen = install_pipeline(monkeypatch, fake_result())
    runner.handle(
        parse_command("design a rail --datasheet TPS=https://x.test/d.pdf"),
        channel="C1",
        thread_ts="1.1",
    )
    assert seen["datasheets"] == {"TPS": "https://x.test/d.pdf"}
    assert seen["time_limit_s"] == 20.0
    assert seen["review"] is True


def test_place_asks_the_pipeline_to_skip_the_review(setup, monkeypatch):
    transport, runner = setup
    seen = install_pipeline(monkeypatch, fake_result())
    runner.handle(parse_command("place a rail"), channel="C1", thread_ts="1.1")
    assert seen["review"] is False
    assert "skipped" in posted(transport)


def test_blocking_findings_reach_the_channel(setup, monkeypatch):
    transport, runner = setup
    install_pipeline(
        monkeypatch,
        fake_result(findings=[finding(Severity.BLOCKER, "EN pin floats")]),
    )
    runner.handle(parse_command("design a rail"), channel="C1", thread_ts="1.1")
    assert "EN pin floats" in posted(transport)
    assert ":red_circle:" in posted(transport)


def test_review_without_a_prior_run_says_so(setup):
    transport, runner = setup
    runner.handle(Command(verb="review"), channel="C1", thread_ts="1.1")
    assert "don't have a finished run in this thread" in posted(transport)


def test_review_reruns_the_critic_on_the_threads_run(setup, monkeypatch):
    transport, runner = setup
    install_pipeline(monkeypatch, fake_result())
    runner.handle(parse_command("place a rail"), channel="C1", thread_ts="1.1")

    monkeypatch.setattr(
        # ``review_circuit`` answers ``(findings, dropped)``. This double
        # returned the bare list, which is how the caller's unpacking bug
        # (a list and a tuple reported as two findings) survived a green
        # suite: the stub agreed with the caller instead of with the
        # function.
        R, "review_circuit", lambda model, spec, facts=None: (
            [finding(Severity.MARGINAL, "electrolytic ESR is too high")],
            (),
        )
    )
    runner.handle(Command(verb="review"), channel="C1", thread_ts="1.1")
    assert "electrolytic ESR is too high" in posted(transport)


def test_a_failed_critic_does_not_post_a_clean_review(setup, monkeypatch):
    """The critic answered something unreadable. The thread must say that.

    A hardware team reading "Review found nothing to flag" over a model
    failure is the whole defect: nothing was flagged because nothing was read.
    """
    from silkscreen.agents.review import ReviewError

    transport, runner = setup
    install_pipeline(monkeypatch, fake_result())
    runner.handle(parse_command("place a rail"), channel="C1", thread_ts="1.1")

    def unreadable(model, spec, facts=None):
        raise ReviewError("the critic's answer had no 'findings' list")

    monkeypatch.setattr(R, "review_circuit", unreadable)
    runner.handle(Command(verb="review"), channel="C1", thread_ts="1.1")
    messages = posted(transport)
    assert "nothing to flag" not in messages
    assert "Review inconclusive" in messages


def test_a_failed_critic_does_not_erase_the_previous_findings(setup, monkeypatch):
    """Overwriting real findings with [] would turn a model outage into a
    board that had blockers a moment ago and reports none now."""
    from silkscreen.agents.review import ReviewError

    transport, runner = setup
    install_pipeline(
        monkeypatch,
        fake_result(findings=[finding(Severity.BLOCKER, "EN pin floats")]),
    )
    runner.handle(parse_command("place a rail"), channel="C1", thread_ts="1.1")

    def unreadable(model, spec, facts=None):
        raise ReviewError("not JSON")

    monkeypatch.setattr(R, "review_circuit", unreadable)
    runner.handle(Command(verb="review"), channel="C1", thread_ts="1.1")
    record = runner._store.get(("C1", "1.1"))
    assert [f.title for f in record.result.findings] == ["EN pin floats"]
    assert record.result.review.failed


def test_a_run_is_remembered_per_thread_not_per_channel(setup, monkeypatch):
    transport, runner = setup
    install_pipeline(monkeypatch, fake_result())
    runner.handle(parse_command("design a rail"), channel="C1", thread_ts="1.1")
    runner.handle(Command(verb="order"), channel="C1", thread_ts="2.2")
    assert "don't have a finished run in this thread" in posted(transport)


def test_order_posts_a_draft_and_never_submits(setup, monkeypatch):
    transport, runner = setup
    install_pipeline(monkeypatch, fake_result())
    runner.handle(parse_command("design a rail"), channel="C1", thread_ts="1.1")
    runner.handle(parse_command("order 25"), channel="C1", thread_ts="1.1")

    messages = posted(transport)
    assert "Fabrication order — draft" in messages
    assert "No order has been placed" in messages
    attached = transport.calls("files.completeUploadExternal")[-1]
    assert attached["files"][0]["title"].startswith("order-draft-")
    # Nothing left this process except Slack calls.
    for request in transport.requests:
        assert "slack" in request.url


def test_a_model_error_is_reported_as_a_setup_problem(setup, monkeypatch):
    transport, runner = setup

    def boom(*args, **kwargs):
        raise ModelError("401 from the provider")

    monkeypatch.setattr(R, "generate_pcb", boom)
    runner.handle(parse_command("design a rail"), channel="C1", thread_ts="1.1")
    messages = posted(transport)
    assert "The model call failed" in messages
    assert "GOOGLE_API_KEY" in messages


def test_an_unexpected_failure_does_not_kill_the_worker(setup, monkeypatch):
    transport, runner = setup

    def boom(*args, **kwargs):
        raise RuntimeError("something internal")

    monkeypatch.setattr(R, "generate_pcb", boom)
    runner.handle(parse_command("design a rail"), channel="C1", thread_ts="1.1")
    assert "That run failed" in posted(transport)


def test_a_failed_preview_still_posts_the_board_file(setup, monkeypatch):
    """The picture is a convenience; the board file is the deliverable."""
    transport, runner = setup
    install_pipeline(monkeypatch, fake_result())
    monkeypatch.setattr(
        R, "render_board", lambda *a, **k: (_ for _ in ()).throw(ValueError("nope"))
    )
    runner.handle(parse_command("design a rail"), channel="C1", thread_ts="1.1")
    titles = [
        c["files"][0]["title"] for c in transport.calls("files.completeUploadExternal")
    ]
    assert titles == ["board.kicad_pcb"]


def test_concurrency_slots_are_bounded(setup):
    transport, runner = setup
    assert runner.acquire_slot(timeout=1)
    assert runner.acquire_slot(timeout=1)
    assert not runner.acquire_slot(timeout=0.05)  # default max is 2
    runner.release_slot()
    assert runner.acquire_slot(timeout=1)


def test_progress_updates_are_throttled(setup, monkeypatch):
    """Slack rate-limits chat.update; a run has more events than a reader has
    attention."""
    transport, runner = setup
    install_pipeline(monkeypatch, fake_result())
    runner.handle(parse_command("design a rail"), channel="C1", thread_ts="1.1")
    # Four stage events fired, but they arrive inside one throttle window, so
    # only the first and the final `finish` push an edit.
    assert len(transport.calls("chat.update")) <= 2


def test_a_dropped_progress_edit_does_not_abort_the_run(monkeypatch, tmp_path):
    """generate_pcb treats a callback exception as a cancel."""
    transport = RecordingTransport(
        {
            "chat.update": {"ok": False, "error": "message_not_found"},
            "getUploadURLExternal": {
                "ok": True,
                "upload_url": "https://files.slack.test/u",
                "file_id": "F1",
            },
        }
    )
    config = Config(**CONFIG_KWARGS, workdir=tmp_path / "runs")
    runner = R.Runner(
        config, SlackClient("t", transport=transport, max_retries=0),
        model_factory=lambda: object(),
    )
    install_pipeline(monkeypatch, fake_result())
    runner.handle(parse_command("design a rail"), channel="C1", thread_ts="1.1")
    assert "Board ready" in json.dumps(transport.calls("chat.postMessage"))


def test_run_store_is_bounded():
    store = R.RunStore(limit=2)
    for index in range(4):
        store.put(
            ("C1", str(index)),
            R.RunRecord(run_id=str(index), command=Command("help"), result=None,
                        finished_at=float(index)),
        )
    assert store.get(("C1", "0")) is None
    assert store.get(("C1", "3")) is not None


# -- slash commands, which arrive with no thread to reply in ---------------


def test_a_slash_run_anchors_its_own_thread(setup, monkeypatch):
    """Greptile P1 on PR #13.

    A slash command has no message to thread under, so every run in a channel
    was stored under the key ``(channel, "")`` and posted with an empty
    thread_ts. Two people running one in the same channel then shared a run
    record, and a later `order` could describe someone else's board.
    """
    transport, runner = setup
    install_pipeline(monkeypatch, fake_result())
    runner.handle(parse_command("design a rail"), channel="C1", thread_ts="")

    calls = transport.calls("chat.postMessage")
    # The first message is the anchor: top-level, with no thread_ts at all.
    assert "thread_ts" not in calls[0]
    # Everything after it hangs off that anchor's ts, not off "".
    anchor = "1700000000.000100"
    assert [c.get("thread_ts") for c in calls[1:]] == [anchor] * (len(calls) - 1)
    for call in transport.calls("files.completeUploadExternal"):
        assert call["thread_ts"] == anchor


def test_two_slash_runs_in_one_channel_do_not_share_a_record(setup, monkeypatch):
    transport, runner = setup
    install_pipeline(monkeypatch, fake_result())
    runner.handle(parse_command("design a rail"), channel="C1", thread_ts="")
    # The stored key is the anchor's ts, never the empty string.
    assert runner._store.get(("C1", "")) is None
    assert runner._store.get(("C1", "1700000000.000100")) is not None


def test_follow_up_verbs_are_refused_outside_a_thread(setup):
    """`order` as a slash command has no run it could honestly refer to."""
    transport, runner = setup
    runner.handle(Command(verb="order"), channel="C1", thread_ts="")
    messages = posted(transport)
    assert "in the thread of a design run" in messages
    assert "Fabrication order" not in messages


def test_a_failing_slash_run_reports_into_its_own_thread(setup, monkeypatch):
    """Greptile P1 on PR #13, second round.

    A slash design anchors its own thread on its first message, but the outer
    error handler only knew the original empty thread_ts — a pipeline failure
    became a top-level channel message while the anchored thread sat stuck on
    its progress message forever.
    """
    transport, runner = setup

    def boom(*args, **kwargs):
        raise ModelError("upstream fell over")

    monkeypatch.setattr(R, "generate_pcb", boom)
    runner.handle(parse_command("design a rail"), channel="C1", thread_ts="")

    calls = transport.calls("chat.postMessage")
    assert "thread_ts" not in calls[0]  # the anchor stays top-level
    anchor = "1700000000.000100"
    # Every message after the anchor — the failure included — hangs off it.
    assert [c.get("thread_ts") for c in calls[1:]] == [anchor] * (len(calls) - 1)
    assert "The model call failed" in posted(transport)


def test_a_failing_threaded_mention_still_reports_into_its_thread(
    setup, monkeypatch
):
    """The anchored-failure fix must not swallow the mention path: a run that
    already had a thread reports its failure there via the outer handler."""
    transport, runner = setup

    def boom(*args, **kwargs):
        raise ModelError("upstream fell over")

    monkeypatch.setattr(R, "generate_pcb", boom)
    runner.handle(parse_command("design a rail"), channel="C1", thread_ts="42.1")

    calls = transport.calls("chat.postMessage")
    assert calls and all(c.get("thread_ts") == "42.1" for c in calls)
    assert "The model call failed" in posted(transport)

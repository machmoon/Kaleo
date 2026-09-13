"""Message formatting, and the honesty rules it has to keep."""

from __future__ import annotations

import json

from silkscreen.agents.review import ReviewReport, ReviewStatus, Severity

from slackbot import blocks as B
from slackbot.tests.fakes import fake_result, finding


def _text(blocks) -> str:
    # ensure_ascii=False so an em dash or a × survives as itself; the default
    # would turn every assertion in this file into one about \u escapes.
    return json.dumps(blocks, ensure_ascii=False)


def test_result_counts_come_from_the_result():
    result = fake_result()
    rendered = _text(B.result_blocks(result))
    assert "*2* parts" in rendered
    assert "20.00 × 12.00 mm" in rendered
    assert "FEASIBLE" in rendered


def test_every_block_stays_inside_slacks_limits():
    result = fake_result(
        findings=[finding(title="x" * 5000, detail="y" * 9000) for _ in range(40)]
    )
    blocks = B.result_blocks(result)
    assert len(blocks) <= B.MAX_BLOCKS
    for block in blocks:
        if block["type"] == "section":
            assert len(block["text"]["text"]) <= B.MAX_SECTION_CHARS


def test_findings_are_ordered_worst_first():
    findings = [
        finding(Severity.NOTE, "a note"),
        finding(Severity.BLOCKER, "a blocker"),
        finding(Severity.MARGINAL, "a marginal"),
    ]
    rendered = _text(B.findings_blocks(findings))
    assert rendered.index("a blocker") < rendered.index("a marginal")
    assert rendered.index("a marginal") < rendered.index("a note")


def test_an_empty_review_says_so():
    assert "nothing to flag" in _text(B.findings_blocks([]))


def test_a_failed_review_is_not_reported_as_a_clean_one():
    """A green tick over a critic that answered gibberish is the same lie as
    "board ready" over a ratsnest: it states something nobody established."""
    failed = ReviewReport(status=ReviewStatus.FAILED, detail="not JSON")
    rendered = _text(B.findings_blocks([], report=failed))
    assert "nothing to flag" not in rendered
    assert "white_check_mark" not in rendered
    assert "Review inconclusive" in rendered


def test_the_thread_card_carries_the_failed_review_through():
    result = fake_result(
        review=ReviewReport(status=ReviewStatus.FAILED, detail="not JSON")
    )
    rendered = _text(B.result_blocks(result, reviewed=True))
    assert "nothing to flag" not in rendered
    assert "Review inconclusive" in rendered
    # The headline is the only line most readers see; it must not say
    # "ready" above a review that established nothing.
    assert "Board ready" not in rendered
    assert "review inconclusive" in rendered


def test_a_clean_review_still_says_it_found_nothing():
    """The fix must not turn every empty review into an inconclusive one."""
    result = fake_result(review=ReviewReport(status=ReviewStatus.OK))
    rendered = _text(B.result_blocks(result, reviewed=True))
    assert "nothing to flag" in rendered
    assert "Board ready" in rendered


def test_a_skipped_review_is_not_reported_as_a_clean_one():
    """"No findings" and "the review did not run" are different facts."""
    rendered = _text(B.result_blocks(fake_result(), reviewed=False))
    assert "nothing to flag" not in rendered
    assert "skipped" in rendered


def test_model_text_is_escaped_into_mrkdwn():
    rendered = _text(
        B.findings_blocks([finding(title="VIN < 3.3V & VOUT > VIN")])
    )
    assert "&lt;" in rendered and "&gt;" in rendered and "&amp;" in rendered


def test_only_received_stages_are_ticked():
    blocks = B.progress_blocks("an led", done=["read"], current="propose", elapsed_s=4)
    rendered = _text(blocks)
    assert ":white_check_mark: read datasheets" in rendered
    assert ":hourglass_flowing_sand: *propose a circuit*" in rendered
    # Nothing downstream may be shown as done before its event arrived.
    assert ":white_check_mark: place the board" not in rendered
    assert "4s elapsed" in rendered


def test_progress_with_no_events_ticks_nothing():
    rendered = _text(B.progress_blocks("an led", done=[], current="", elapsed_s=1))
    assert ":white_check_mark:" not in rendered


def test_warnings_surface():
    result = fake_result(board=fake_result().board)
    result.board.warnings = ["fell back to shelf packing"]
    assert "shelf packing" in _text(B.result_blocks(result))


def test_repair_rounds_are_reported_when_they_happened():
    quiet = fake_result()
    noisy = fake_result(attempts=[object(), object(), object()])
    assert "repair round" not in _text(B.result_blocks(quiet))
    assert "2 repair round(s)" in _text(B.result_blocks(noisy))


def test_long_part_lists_are_capped_and_counted():
    result = fake_result()
    result.board.parts = result.board.parts * 30
    rendered = _text(B.result_blocks(result))
    assert f"…and {60 - B.MAX_LISTED_PARTS} more" in rendered


def test_truncate_marks_what_it_cut():
    assert B.truncate("abcdef", 4).endswith("…")
    assert B.truncate("abc", 10) == "abc"


def test_a_ratsnest_is_never_announced_as_a_ready_board():
    """The headline a channel reads must not claim copper that is not there.

    `googleapps/chat.py::verdict` has always ranked the ratsnest; this card
    read the review alone, so a board with unfinished nets was posted as
    ":white_check_mark: Board ready" and no unrouted net appeared anywhere a
    Slack client renders (the plain-text fallback carries them, but Slack
    does not display `text` when `blocks` are present).
    """
    from slackbot.tests.fakes import FakeRoute

    result = fake_result(
        route=FakeRoute(
            unrouted={
                "VCC": "no path within the expansion budget",
                "GND": "ripped up to free a channel for VCC",
            }
        )
    )
    rendered = json.dumps(B.result_blocks(result))
    assert "Board ready" not in rendered
    assert "2 nets left unrouted" in rendered
    # Every net, by name, with the router's own reason.
    assert "VCC" in rendered and "GND" in rendered
    assert "no path within the expansion budget" in rendered
    assert "ripped up to free a channel" in rendered


def test_routing_turned_off_is_placed_not_ready():
    """Nothing routed is not the same as everything routed."""
    rendered = json.dumps(B.result_blocks(fake_result(route=None)))
    assert "Board ready" not in rendered
    assert "not routed" in rendered

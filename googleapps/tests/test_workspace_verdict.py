"""The verdict and the deterministic agenda over a review that never ran.

Two producers hand this package a result: the engine's ``PipelineResult``
(a ``ReviewReport`` on ``review``) and the service's ``SessionResult`` (a
``reviewed`` flag and no ``review`` attribute at all). Both must be read, and
neither may be rendered as "no findings" or "board ready" when the critic was
never asked, nor as "ready" when routing was turned off.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from silkscreen.agents.review import ReviewReport, ReviewStatus, Severity

from googleapps import chat, specreview
from googleapps.tests.fakes import (
    FakeSpec,
    RecordingTransport,
    fake_board,
    fake_result,
    fake_route,
    finding,
)
from googleapps.transport import GoogleError

NOW = 1_756_684_800.0


@dataclass
class SessionShaped:
    """The attributes ``service.deliver.SessionResult`` exposes, and no
    ``review`` attribute -- a step session knows only whether the review
    step was pressed."""

    intent: str = "a 3.3V regulator"
    spec: FakeSpec = field(default_factory=FakeSpec)
    board: Any = field(default_factory=fake_board)
    route: Any = field(default_factory=fake_route)
    findings: list[Any] = field(default_factory=list)
    board_path: Path = Path("board.kicad_pcb")
    reviewed: bool = True
    done: tuple[str, ...] = ("place", "route")
    spec_review: Any = None

    @property
    def blockers(self) -> list[Any]:
        return [f for f in self.findings if str(f.severity.value) == "blocker"]


def card_text(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


# -- review_state reads both producers -------------------------------------


def test_review_state_reads_the_pipelines_report_and_the_sessions_flag():
    assert chat.review_state(fake_result()) == "ok"
    assert chat.review_state(fake_result(review=ReviewReport())) == "skipped"
    assert (
        chat.review_state(fake_result(review=ReviewReport(status=ReviewStatus.FAILED)))
        == "failed"
    )
    assert chat.review_state(SessionShaped(reviewed=True)) == "ok"
    assert chat.review_state(SessionShaped(reviewed=False)) == "skipped"


def test_a_result_that_says_nothing_about_its_review_is_not_reviewed():
    """"Nobody said the critic ran" is not evidence that it did."""

    @dataclass
    class Bare:
        findings: list[Any] = field(default_factory=list)
        blockers: list[Any] = field(default_factory=list)
        route: Any = field(default_factory=fake_route)

    assert chat.review_state(Bare()) == "skipped"
    assert chat.verdict(Bare()) == "not reviewed — the review step has not run"


# -- the verdict and the card over a session that never pressed review -----


def test_an_unreviewed_session_is_never_verdicted_ready_or_no_findings():
    result = SessionShaped(reviewed=False)
    assert chat.verdict(result) == "not reviewed — the review step has not run"
    text = card_text(chat.run_card(result))
    assert "board ready" not in text
    assert "no findings" not in text
    assert "not reviewed (the review step has not run)" in text


def test_an_unreviewed_session_outranks_finished_copper_and_a_ratsnest():
    clean = SessionShaped(reviewed=False, route=fake_route())
    torn = SessionShaped(
        reviewed=False, route=fake_route(unrouted={"VOUT": "no path"})
    )
    assert chat.verdict(clean) == "not reviewed — the review step has not run"
    assert chat.verdict(torn) == "not reviewed — the review step has not run"
    # The ratsnest is still named on the card; the headline just does not
    # pretend the review happened.
    assert "unrouted VOUT: no path" in card_text(chat.run_card(torn))


def test_blockers_still_outrank_everything_on_a_session():
    result = SessionShaped(reviewed=True, findings=[finding()], route=None)
    assert chat.verdict(result) == "needs review — 1 blocker(s)"


def test_a_reviewed_session_with_routing_turned_off_is_placed_not_ready():
    result = SessionShaped(reviewed=True, route=None)
    assert chat.verdict(result) == "placed — not routed (routing was turned off)"
    text = card_text(chat.run_card(result))
    assert "board ready" not in text
    assert "not routed (routing was turned off for this run)" in text


def test_a_reviewed_clean_fully_routed_session_is_board_ready():
    """The fix must not make every session read as unreviewed."""
    assert chat.verdict(SessionShaped(reviewed=True)) == "board ready"


# -- agenda_from_result: three outcomes, three answers ---------------------


def test_the_agenda_from_a_pipeline_result_whose_review_was_skipped():
    """``PipelineResult`` has no ``reviewed`` attribute; the report is the
    fact, and a skipped one must not read as "0 blocking finding(s)"."""
    result = fake_result(review=ReviewReport(), route=fake_route())
    built = specreview.agenda_from_result(result, board_name="ldo")
    assert built.reviewed is False
    assert built.skip_reason == specreview.NOT_REVIEWED_REASON
    assert "review step has not run" in built.skip_reason
    assert "review" not in built.prepared_from
    assert "route" in built.prepared_from
    assert "0 blocking finding(s)" not in built.summary
    assert "the review did not run" in built.summary


def test_the_agenda_from_a_pipeline_result_whose_review_failed():
    result = fake_result(
        review=ReviewReport(status=ReviewStatus.FAILED, detail="not JSON")
    )
    built = specreview.agenda_from_result(result, board_name="ldo")
    assert built.reviewed is False
    assert built.skip_reason == specreview.NOT_REVIEWED_REASON
    assert "failed to produce a readable answer (not JSON)" in built.summary


def test_the_agenda_from_an_unreviewed_session_result():
    built = specreview.agenda_from_result(
        SessionShaped(reviewed=False), board_name="ldo"
    )
    assert built.reviewed is False
    assert built.skip_reason == specreview.NOT_REVIEWED_REASON
    assert "review" not in built.prepared_from
    assert "not reviewed (the review step has not run)" in built.summary


def test_an_unreviewed_ratsnest_is_still_not_booked():
    """A ratsnest is a blocking item, but a meeting about an unreviewed board
    would decide on evidence nobody has; the review skip comes first."""
    built = specreview.agenda_from_result(
        SessionShaped(reviewed=False, route=fake_route(unrouted={"VOUT": "no path"})),
        board_name="ldo",
    )
    assert built.blocking  # the ratsnest is on the agenda
    assert built.skip_reason == specreview.NOT_REVIEWED_REASON


def test_a_review_that_found_nothing_is_a_real_answer():
    for result in (fake_result(), SessionShaped(reviewed=True)):
        built = specreview.agenda_from_result(result, board_name="ldo")
        assert built.reviewed is True
        assert built.blocking == ()
        assert built.skip_reason == specreview.NO_BLOCKERS_REASON
        assert "review" in built.prepared_from
        assert "0 blocking finding(s)" in built.summary


def test_a_review_that_found_blockers_books():
    for result in (
        fake_result(findings=[finding()]),
        SessionShaped(reviewed=True, findings=[finding()]),
    ):
        built = specreview.agenda_from_result(result, board_name="ldo")
        assert built.reviewed is True
        assert len(built.blocking) == 1
        assert built.skip_reason is None
        assert "1 blocking finding(s)" in built.summary


def test_the_summary_says_not_routed_rather_than_zero_unrouted():
    built = specreview.agenda_from_result(fake_result(route=None), board_name="ldo")
    assert "not routed (routing was turned off)" in built.summary
    assert "0 net(s) unrouted" not in built.summary
    assert "route" not in built.prepared_from


def test_an_agenda_from_a_real_spec_review_is_reviewed_by_construction():
    built = specreview.agenda_from_spec_review(
        {"title": "t", "items": [{"topic": "x", "blocking": True}]}
    )
    assert built.reviewed is True
    assert built.skip_reason is None


# -- schedule_spec_review refuses an unreviewed agenda before any request --


def test_booking_an_unreviewed_agenda_is_refused_before_any_request():
    built = specreview.agenda_from_result(
        SessionShaped(reviewed=False, route=fake_route(unrouted={"VOUT": "no path"})),
        board_name="ldo",
    )
    transport = RecordingTransport()
    with pytest.raises(GoogleError, match="review step has not run") as excinfo:
        specreview.schedule_spec_review(
            "ya29.token",
            agenda=built,
            attendees=["lead@example.com"],
            transport=transport,
            now=NOW,
        )
    assert excinfo.value.code == "not_reviewed"
    assert transport.requests == []


def test_a_note_only_review_stays_bookable_nowhere_but_says_so():
    """A reviewed board with a note and no blocker: nothing blocks, nothing
    is booked, and the reason is the no-blockers one, not the unreviewed
    one."""
    built = specreview.agenda_from_result(
        fake_result(findings=[finding(Severity.NOTE)]), board_name="ldo"
    )
    assert built.skip_reason == specreview.NO_BLOCKERS_REASON
    with pytest.raises(GoogleError, match="nothing needs a meeting"):
        specreview.schedule_spec_review(
            "ya29.token",
            agenda=built,
            attendees=["lead@example.com"],
            transport=RecordingTransport(),
            now=NOW,
        )

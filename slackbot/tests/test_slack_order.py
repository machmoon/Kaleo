"""The order draft, and the line it must not cross.

The most important tests in this file are the negative ones: nothing in the
package submits an order, contacts a vendor, or touches a payment path, and
that has to be enforced rather than intended.
"""

from __future__ import annotations

import ast
import json
import pathlib

import pytest
from silkscreen.agents.review import ReviewReport, ReviewStatus, Severity

from slackbot import order as O
from slackbot.tests.fakes import fake_result, finding


def test_draft_carries_the_boards_real_geometry():
    draft = O.prepare_order(fake_result(), quantity=10)
    assert (draft.width_mm, draft.height_mm) == (20.0, 12.0)
    assert draft.area_mm2 == 240.0
    assert draft.total_area_mm2 == 2400.0
    assert draft.quantity == 10


def test_a_blocking_finding_makes_the_draft_not_ready():
    result = fake_result(
        findings=[finding(Severity.BLOCKER, "EN pin floats", parts=("U1",))]
    )
    draft = O.prepare_order(result)
    assert draft.blockers == ["EN pin floats [U1]"]
    assert draft.ready is False
    assert "EN pin floats" in json.dumps(O.draft_json(draft))


def test_an_unread_review_is_named_as_a_gap_not_treated_as_no_blockers():
    """No blockers is only evidence when the critic delivered a verdict."""
    result = fake_result(
        review=ReviewReport(status=ReviewStatus.FAILED, detail="not JSON")
    )
    draft = O.prepare_order(result)
    assert draft.blockers == []
    assert draft.ready is False
    text = json.dumps(O.draft_json(draft))
    assert "readable verdict" in text
    assert "not evidence" in text


def test_a_clean_review_adds_no_review_gap():
    draft = O.prepare_order(fake_result())
    assert not any("readable verdict" in gap for gap in draft.gaps)


def test_notes_and_marginals_do_not_block():
    result = fake_result(findings=[finding(Severity.NOTE, "consider a test point")])
    assert O.prepare_order(result).blockers == []


def test_a_clean_board_is_still_not_ready_while_files_are_missing():
    """Placement is not fabrication. Saying otherwise would be the lie."""
    draft = O.prepare_order(fake_result(), artifacts=["board.kicad_pcb"])
    assert draft.blockers == []
    assert draft.ready is False
    gaps = " ".join(draft.gaps).lower()
    assert "gerber" in gaps and "routing" in gaps and "bill of materials" in gaps


def test_a_run_with_no_board_file_says_so():
    assert any("No board file" in gap for gap in O.prepare_order(fake_result()).gaps)


def test_json_states_the_negatives_explicitly():
    payload = O.draft_json(O.prepare_order(fake_result()))
    assert payload["submitted"] is False
    assert payload["payment"] is None
    assert payload["ready_to_order"] is False
    assert "No order has been placed" in payload["notice"]


def test_blocks_repeat_the_not_submitted_notice_verbatim():
    rendered = json.dumps(
        O.draft_blocks(O.prepare_order(fake_result(), quantity=3)), ensure_ascii=False
    )
    assert O.NOT_SUBMITTED in rendered
    assert "3* boards" in rendered


def test_quantity_must_be_positive():
    with pytest.raises(ValueError):
        O.prepare_order(fake_result(), quantity=0)


def test_stackup_defaults_can_be_overridden():
    draft = O.prepare_order(fake_result(), stackup={"layers": 4})
    assert draft.stackup["layers"] == 4
    assert draft.stackup["thickness_mm"] == O.DEFAULT_STACKUP["thickness_mm"]


def test_only_the_slack_client_can_reach_the_network():
    """A structural guard on the ordering boundary, not a formality.

    Ordering boards spends money and commits a physical artefact, so the rule
    is that this package can talk to exactly one host: Slack. Enforcing it by
    import rather than by keyword means a future vendor call cannot be added
    quietly -- it has to either import a network module here, which fails this
    test, or go through the Slack client, which cannot reach a fab.
    """
    package = pathlib.Path(__file__).resolve().parents[1]
    network = ("urllib.request", "http.client", "requests", "socket", "httpx")

    for path in sorted(package.glob("*.py")):
        # encoding is explicit: this package's sources contain smart quotes,
        # and Windows would otherwise read them as cp1252 and raise.
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        offenders = imported.intersection(network)
        assert not offenders or path.name == "slack.py", (
            f"{path.name} imports {offenders}; only slack.py may reach the network"
        )


def test_the_slack_client_talks_to_slack_and_nowhere_else():
    from slackbot import slack

    assert slack.API_ROOT == "https://slack.com/api/"

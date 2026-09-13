"""The inbox: ideas from Slack waiting for the Ada desktop to accept one.

Unit tests drive :class:`service.inbox.Inbox` with a fake clock; the route tests
drive the real server, because the bridge and the overlay are two different
processes and the HTTP shape is the whole contract between them.
"""

from __future__ import annotations

import json

import pytest

from service import inbox as _inbox
from service.tests.test_app import get, post  # noqa: F401
from service.tests.test_app import server as _test_app_server  # noqa: F401


@pytest.fixture
def server(_test_app_server):  # noqa: F811
    return _test_app_server


class Clock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def _fresh_inbox(monkeypatch):
    monkeypatch.setattr(_inbox, "INBOX", _inbox.Inbox())


def _add(inbox, text="a 3.3V LDO board", key="slack:T1:C1:1.0"):
    return inbox.add(text, source="slack", key=key, reply_to={"channel": "C1"})


def test_one_message_delivered_twice_is_one_idea():
    """A Socket Mode redelivery must not become a second paid run."""
    inbox = _inbox.Inbox()
    first, created = _add(inbox)
    again, created_again = _add(inbox)
    assert created and not created_again
    assert first.id == again.id
    assert len(inbox.list()) == 1


def test_accept_is_exclusive_and_repeatable_by_the_same_desktop():
    inbox = _inbox.Inbox()
    idea, _ = _add(inbox)
    inbox.accept(idea.id, "laptop")
    assert inbox.accept(idea.id, "laptop").state == "accepted"
    with pytest.raises(_inbox.IdeaConflict, match="accepted by laptop"):
        inbox.accept(idea.id, "desktop")


def test_only_the_claimant_may_start_it_and_start_records_the_session():
    inbox = _inbox.Inbox()
    idea, _ = _add(inbox)
    with pytest.raises(_inbox.IdeaConflict):
        inbox.start(idea.id, "laptop", "s1")  # never accepted
    inbox.accept(idea.id, "laptop")
    with pytest.raises(_inbox.IdeaConflict):
        inbox.start(idea.id, "desktop", "s1")
    started = inbox.start(idea.id, "laptop", "s1")
    assert (started.state, started.session) == ("started", "s1")


def test_a_pending_idea_expires_with_a_reason_rather_than_vanishing():
    clock = Clock()
    inbox = _inbox.Inbox(clock=clock)
    idea, _ = _add(inbox)
    clock.now += _inbox.PENDING_TTL_S + 1
    got = inbox.get(idea.id)
    assert got.state == "expired"
    assert "30 minutes" in got.detail
    with pytest.raises(_inbox.IdeaConflict):
        inbox.accept(idea.id, "laptop")


def test_an_accepted_idea_that_never_starts_is_failed_not_left_picked_up():
    clock = Clock()
    inbox = _inbox.Inbox(clock=clock)
    idea, _ = _add(inbox)
    inbox.accept(idea.id, "laptop")
    clock.now += _inbox.ACCEPT_TTL_S + 1
    assert inbox.get(idea.id).state == "failed"


def test_the_inbox_is_bounded_and_drops_settled_ideas_first():
    inbox = _inbox.Inbox(limit=2)
    old, _ = _add(inbox, key="k1")
    inbox.accept(old.id, "laptop")
    inbox.start(old.id, "laptop", "s")
    pending, _ = _add(inbox, key="k2")
    _add(inbox, key="k3")
    ids = [i.id for i in inbox.list()]
    assert old.id not in ids and pending.id in ids


def test_text_is_required_and_bounded():
    inbox = _inbox.Inbox()
    with pytest.raises(_inbox.InboxError):
        inbox.add("   ", source="slack")
    with pytest.raises(_inbox.InboxError):
        inbox.add("x" * (_inbox.MAX_TEXT_CHARS + 1), source="slack")


# ------------------------------------------------------------------ routes


def test_the_full_hand_off_over_http(server):
    status, idea = post(
        server,
        {
            "text": "a 555 blinker",
            "source": "slack",
            "key": "slack:T:C:9.9",
            "reply_to": {"channel": "C", "thread_ts": "9.9"},
            "user": "U1",
        },
        path="/inbox",
    )
    assert status == 201 and idea["state"] == "pending"
    status, again = post(
        server,
        {"text": "a 555 blinker", "source": "slack", "key": "slack:T:C:9.9"},
        path="/inbox",
    )
    assert status == 200 and again["id"] == idea["id"]

    status, headers, body = get(server, "/inbox")
    assert status == 200
    assert headers["Cache-Control"] == "no-store"
    assert [i["id"] for i in json.loads(body)["ideas"]] == [idea["id"]]

    base = f"/inbox/{idea['id']}"
    assert post(server, {"claimant": "laptop"}, path=base + "/accept")[0] == 200
    status, conflict = post(server, {"claimant": "other"}, path=base + "/accept")
    assert status == 409 and "accepted by laptop" in conflict["error"]
    # An accepted idea is no longer offered to anyone.
    assert json.loads(get(server, "/inbox")[2])["ideas"] == []

    status, started = post(
        server, {"claimant": "laptop", "session": "sess1"}, path=base + "/start"
    )
    assert status == 200 and started["session"] == "sess1"
    status, _, body = get(server, base)
    assert status == 200 and json.loads(body)["state"] == "started"


def test_route_errors_are_400_404_409(server):
    assert post(server, {"source": "slack"}, path="/inbox")[0] == 400
    assert post(server, {"text": "x"}, path="/inbox")[0] == 400  # no source
    assert get(server, "/inbox/idea_" + "0" * 32)[0] == 404
    assert get(server, "/inbox/../steps")[0] == 404
    status, idea = post(server, {"text": "x", "source": "slack"}, path="/inbox")
    assert (
        post(
            server, {"claimant": "a", "session": "s"}, path=f"/inbox/{idea['id']}/start"
        )[0]
        == 409
    )
    assert post(server, {}, path=f"/inbox/{idea['id']}/accept")[0] == 400
    assert post(server, {"claimant": "a"}, path=f"/inbox/{idea['id']}/nope")[0] == 404

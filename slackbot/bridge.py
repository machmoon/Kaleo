"""Slack -> Ada on the laptop: say an idea in Slack, the desktop starts the design.

``python -m slackbot socket`` runs this. It is the Socket Mode sibling of
:mod:`slackbot.app`, with one difference that decides everything else: it does
**not** run the pipeline. :mod:`slackbot.app` generates a whole board inside
the bot process and uploads it; this bridge hands the sentence to the Ada
overlay on the same laptop, which starts the ordinary approval-gated step run
with it -- so the engineer sees it in KiCad and presses every step after the
first, exactly as if they had typed the sentence into the bar.

The path, end to end::

    Slack message --(Socket Mode)--> bridge --POST /inbox--> service
    overlay --GET /inbox, POST /inbox/<id>/accept--> service
    overlay --POST /steps (propose)--> service, then POST /inbox/<id>/start
    bridge --GET /inbox/<id>, GET /steps/<session>--> thread replies

The bridge only ever *reads* the run (``GET /steps/<id>``, which "never runs a
stage and never costs anything" -- ``app/src/lib/silkscreen/client.ts::
stepStatus``). The Slack tokens stay in this process; the service never learns
them and the overlay never talks to Slack.

What one message costs, stated rather than implied: the overlay's first step,
``propose``, reads, plans and proposes a circuit -- model calls nobody pressed
a button on the laptop for. Every later step still waits for a press. That is
the feature (the user asked for "I send it and Ada starts working"), and it is
why ``SILKSCREEN_SLACK_USERS`` exists: a workspace can install the app and
still keep who may spend on this laptop to a named list.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.parse
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .commands import strip_slack_markup
from .slack import HttpRequest, SlackClient, SlackError, Transport, urllib_transport
from .socket_mode import SocketRequest

__all__ = [
    "Bridge",
    "BridgeConfig",
    "EngineClient",
    "load_bridge_config",
    "message_key",
    "DEFAULT_ENGINE_URL",
]

log = logging.getLogger("slackbot.bridge")

#: The address ``silkscreen serve`` and the desktop app both default to
#: (``app/src/lib/silkscreen/client.ts::DEFAULT_BASE_URL``).
DEFAULT_ENGINE_URL = "http://127.0.0.1:8081"
#: What the service is told to call this source; also the claimant-facing label.
SOURCE = "slack"
POLL_S = 3.0
#: How long an idea may sit unaccepted before the thread is told why.
NUDGE_S = 45.0
#: Consecutive failed polls before the thread hears the engine went away.
UNREACHABLE_POLLS = 10
#: How long one thread is followed. A step run waits on a human between
#: stages; three hours covers an afternoon without following forever.
WATCH_S = 3 * 60 * 60

HELP = (
    "Tell me what to build and I'll hand it to Ada on the laptop — for example "
    "_a 3.3 V LDO board for a sensor, powered from USB-C_. Ada starts the design "
    "there, and you approve each step on the laptop. I'll post progress here."
)


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class BridgeConfig:
    bot_token: str
    app_token: str
    engine_url: str = DEFAULT_ENGINE_URL
    #: The service's bearer gate (``SILKSCREEN_ACCESS_TOKEN``), when one is set.
    engine_token: str = ""
    allowed_channels: frozenset[str] = field(default_factory=frozenset)
    allowed_users: frozenset[str] = field(default_factory=frozenset)

    def redacted(self) -> dict[str, str]:
        return {
            "bot_token": f"<set, {len(self.bot_token)} chars>",
            "app_token": f"<set, {len(self.app_token)} chars>",
            "engine_url": self.engine_url,
            "engine_token": f"<set, {len(self.engine_token)} chars>"
            if self.engine_token
            else "<none>",
            "allowed_channels": ", ".join(sorted(self.allowed_channels)) or "<any>",
            "allowed_users": ", ".join(sorted(self.allowed_users)) or "<any>",
        }


def _set(env: dict[str, str], key: str) -> frozenset[str]:
    return frozenset(p.strip() for p in env.get(key, "").split(",") if p.strip())


def load_bridge_config(
    env: dict[str, str] | None = None, *, dotenv: bool = True
) -> BridgeConfig:
    """Read the bridge's settings, naming every missing one at once.

    Unlike :func:`slackbot.config.load_config` this needs neither
    ``SLACK_SIGNING_SECRET`` (Socket Mode has no inbound HTTP to sign) nor
    ``GOOGLE_API_KEY`` (the service spends, not the bridge).
    """
    if env is None:
        if dotenv:
            from .config import _load_dotenv  # engine path set up by config

            _load_dotenv(Path.cwd() / ".env")
        env = dict(os.environ)
    missing = [
        k for k in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN") if not env.get(k, "").strip()
    ]
    if missing:
        raise ConfigError(
            "missing required environment variable(s): "
            + ", ".join(missing)
            + ". SLACK_APP_TOKEN is the xapp- app-level token (connections:write); "
            "see docs in slackbot/bridge.py and .env.example."
        )
    app_token = env["SLACK_APP_TOKEN"].strip()
    if not app_token.startswith("xapp-"):
        raise ConfigError("SLACK_APP_TOKEN must start with xapp- (an app-level token)")
    engine_url = (
        env.get("SILKSCREEN_ENGINE_URL", "").strip() or DEFAULT_ENGINE_URL
    ).rstrip("/")
    parts = urllib.parse.urlsplit(engine_url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ConfigError(
            f"SILKSCREEN_ENGINE_URL is not an http(s) URL: {engine_url!r}"
        )
    return BridgeConfig(
        bot_token=env["SLACK_BOT_TOKEN"].strip(),
        app_token=app_token,
        engine_url=engine_url,
        engine_token=env.get("SILKSCREEN_ACCESS_TOKEN", "").strip(),
        allowed_channels=_set(env, "SILKSCREEN_SLACK_CHANNELS"),
        allowed_users=_set(env, "SILKSCREEN_SLACK_USERS"),
    )


class EngineClient:
    """The three service routes the bridge uses. Answers ``(status, body)``;
    ``status`` 0 means the engine was never reached."""

    def __init__(
        self, base_url: str, token: str = "", *, transport: Transport | None = None
    ):
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._transport = transport or urllib_transport(timeout=10.0)

    def _call(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, Any]]:
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        data = json.dumps(body).encode("utf-8") if body is not None else b""
        try:
            response = self._transport(
                HttpRequest(method, self.base_url + path, headers, data)
            )
        except SlackError as exc:  # urllib_transport's name for URLError
            return 0, {"error": str(exc)}
        except OSError as exc:
            return 0, {"error": str(exc)}
        try:
            parsed = json.loads(response.body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = {}
        return response.status, parsed if isinstance(parsed, dict) else {}

    def post_idea(
        self, text: str, *, key: str, reply_to: dict[str, str], user: str
    ) -> tuple[int, dict[str, Any]]:
        return self._call(
            "POST",
            "/inbox",
            {
                "text": text,
                "source": SOURCE,
                "key": key,
                "reply_to": reply_to,
                "user": user,
            },
        )

    def idea(self, idea_id: str) -> tuple[int, dict[str, Any]]:
        return self._call("GET", "/inbox/" + urllib.parse.quote(idea_id, safe=""))

    def steps(self, session: str) -> tuple[int, dict[str, Any]]:
        return self._call("GET", "/steps/" + urllib.parse.quote(session, safe=""))


def message_key(team: str, channel: str, ts: str) -> str:
    """One Slack message's identity, whichever event type carried it.

    A DM that also @-mentions the bot can arrive as both ``message`` and
    ``app_mention``; keying on the message rather than the event (unlike
    :func:`slackbot.app.delivery_keys`, which includes the type) makes those
    one idea, not two paid runs.
    """
    return f"slack:{team}:{channel}:{ts}"


class _Seen:
    """Bounded set of keys. A local copy of ``slackbot.app._SeenEvents``'s
    idea; importing that module would load the whole pipeline into a process
    that never runs it."""

    def __init__(self, limit: int = 1024):
        self._lock = threading.Lock()
        self._keys: OrderedDict[str, None] = OrderedDict()
        self._limit = limit

    def add_if_new(self, *keys: str) -> bool:
        keys = tuple(k for k in keys if k)
        with self._lock:
            if any(k in self._keys for k in keys):
                return False
            for k in keys:
                self._keys[k] = None
            while len(self._keys) > self._limit:
                self._keys.popitem(last=False)
            return True


def _spawn(work: Callable[[], None]) -> None:
    threading.Thread(target=work, daemon=True).start()


class Bridge:
    """Turns Socket Mode requests into inbox ideas and follows each in its thread."""

    def __init__(
        self,
        config: BridgeConfig,
        slack: SlackClient,
        engine: EngineClient,
        *,
        spawn: Callable[[Callable[[], None]], None] = _spawn,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.config = config
        self.slack = slack
        self.engine = engine
        self._spawn = spawn
        self._sleep = sleep
        self._clock = clock
        self._seen = _Seen()

    # -- inbound ----------------------------------------------------------

    def handle(self, request: SocketRequest) -> None:
        """Called by the socket loop right after the ack. Must not block."""
        if request.type != "events_api":
            return
        payload = request.payload
        if payload.get("type") != "event_callback":
            return
        event = payload.get("event")
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        # Never answer a bot, ourselves included, nor an edit or a join.
        if event.get("bot_id") or event.get("subtype"):
            return
        is_dm = kind == "message" and event.get("channel_type") == "im"
        if kind != "app_mention" and not is_dm:
            return
        channel = str(event.get("channel", ""))
        ts = str(event.get("ts", ""))
        user = str(event.get("user", ""))
        if not channel or not ts:
            return
        key = message_key(str(payload.get("team_id", "")), channel, ts)
        if not self._seen.add_if_new(key, str(payload.get("event_id", ""))):
            log.info("ignoring redelivery %s (retry %s)", key, request.retry_attempt)
            return
        if (
            not is_dm
            and self.config.allowed_channels
            and channel not in self.config.allowed_channels
        ):
            log.info("ignoring mention in disallowed channel %s", channel)
            return
        thread_ts = str(event.get("thread_ts") or ts)
        if self.config.allowed_users and user not in self.config.allowed_users:
            self._spawn(
                lambda: self._say(
                    channel,
                    thread_ts,
                    (
                        "Sorry — only people on this laptop's allow list can start a "
                        "design "
                        "(SILKSCREEN_SLACK_USERS)."
                    ),
                )
            )
            return
        text = strip_slack_markup(str(event.get("text", "")))
        if not text or text.lower() in ("help", "?", "hi", "hello"):
            self._spawn(lambda: self._say(channel, thread_ts, HELP))
            return
        self._spawn(
            lambda: self.deliver(
                text, channel=channel, thread_ts=thread_ts, user=user, key=key
            )
        )

    # -- the hand-off -----------------------------------------------------

    def _say(self, channel: str, thread_ts: str, text: str) -> None:
        try:
            self.slack.post_message(channel, text, thread_ts=thread_ts)
        except SlackError as exc:
            # The thread is the only place anyone would read this; log loudly.
            log.error("could not post to %s: %s", channel, exc)

    def deliver(
        self, text: str, *, channel: str, thread_ts: str, user: str, key: str
    ) -> str | None:
        """Reply, file the idea, and follow it. Returns the idea id, or None."""
        self._say(channel, thread_ts, "On it — handing this to Ada on the laptop.")
        status, body = self.engine.post_idea(
            text,
            key=key,
            reply_to={"channel": channel, "thread_ts": thread_ts},
            user=user,
        )
        if status == 0:
            self._say(
                channel,
                thread_ts,
                (
                    f"I couldn't reach the Ada engine at {self.engine.base_url}. "
                    "Start it "
                    "on the laptop with `silkscreen serve`, then send the idea again."
                ),
            )
            return None
        if status == 401:
            self._say(
                channel,
                thread_ts,
                (
                    "The Ada engine refused this bridge (its SILKSCREEN_ACCESS_TOKEN "
                    "does not match). Nothing was started."
                ),
            )
            return None
        if status not in (200, 201) or not body.get("id"):
            detail = str(body.get("error") or f"HTTP {status}")
            self._say(channel, thread_ts, f"The Ada engine didn't take that: {detail}")
            return None
        idea_id = str(body["id"])
        if status == 200:
            # The inbox already had this message: another bridge process (or
            # this one before a restart) is following it. One follower only.
            log.info("idea %s already filed; not following twice", idea_id)
            return idea_id
        self.watch(idea_id, channel=channel, thread_ts=thread_ts)
        return idea_id

    def watch(self, idea_id: str, *, channel: str, thread_ts: str) -> None:
        """Follow one idea until its run settles, posting each change once."""
        started_at = self._clock()
        nudged = False
        failures = 0
        told_unreachable = False
        state = "pending"
        session = ""
        done: list[str] = []
        waiting_on: tuple[str, ...] = ()

        while self._clock() - started_at < WATCH_S:
            if session:
                status, body = self.engine.steps(session)
            else:
                status, body = self.engine.idea(idea_id)

            if status == 0:
                failures += 1
                if failures >= UNREACHABLE_POLLS and not told_unreachable:
                    told_unreachable = True
                    self._say(
                        channel,
                        thread_ts,
                        (
                            "I've lost contact with the Ada engine on the laptop. "
                            "I'll keep trying for a while; the run itself is "
                            "unaffected if it's still up."
                        ),
                    )
                self._sleep(POLL_S)
                continue
            failures = 0
            if status == 404:
                self._say(
                    channel,
                    thread_ts,
                    (
                        "The Ada engine no longer has this "
                        + ("run" if session else "request")
                        + " — it probably restarted. Send the idea again to start over."
                    ),
                )
                return
            if status != 200:
                self._sleep(POLL_S)
                continue

            if not session:
                new_state = str(body.get("state", ""))
                if new_state == "pending":
                    if not nudged and self._clock() - started_at >= NUDGE_S:
                        nudged = True
                        self._say(
                            channel,
                            thread_ts,
                            (
                                "Still waiting for Ada to pick this up. Is the Kaleo "
                            "app open "
                                "on the laptop? It will start as soon as Ada is free."
                            ),
                        )
                elif new_state in ("failed", "expired"):
                    detail = str(body.get("detail") or new_state)
                    self._say(channel, thread_ts, f"Ada didn't start this: {detail}.")
                    return
                elif new_state == "accepted" and state == "pending":
                    self._say(
                        channel,
                        thread_ts,
                        "Ada picked it up on the laptop and is starting the design.",
                    )
                elif new_state == "started":
                    if state == "pending":
                        self._say(channel, thread_ts, "Ada picked it up on the laptop.")
                    session = str(body.get("session", ""))
                    self._say(
                        channel,
                        thread_ts,
                        (
                            "Design started on the laptop. You approve each step "
                            "there; "
                            "I'll post progress here."
                        ),
                    )
                state = new_state or state
                self._sleep(POLL_S)
                continue

            # Following the step run.
            if body.get("cancelled"):
                self._say(channel, thread_ts, "The run was cancelled on the laptop.")
                return
            now_done = [str(s) for s in body.get("done") or []]
            fresh = [s for s in now_done if s not in done]
            if fresh:
                self._say(channel, thread_ts, "Done: " + ", ".join(fresh) + ".")
                done = now_done
            nxt = tuple(str(s) for s in body.get("next") or [])
            background = body.get("background") or []
            if not nxt:
                files = body.get("files") or {}
                where = f" Files: {', '.join(sorted(files))}." if files else ""
                self._say(
                    channel, thread_ts, "Every step has run on the laptop." + where
                )
                return
            if nxt != waiting_on and not background:
                waiting_on = nxt
                self._say(
                    channel,
                    thread_ts,
                    ("Waiting for you on the laptop — next: " + ", ".join(nxt) + "."),
                )
            self._sleep(POLL_S)

        self._say(
            channel,
            thread_ts,
            ("I've stopped following this thread; the run carries on on the laptop."),
        )


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - entry point
    """``python -m slackbot socket``."""
    from .socket_mode import SocketModeClient

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    try:
        config = load_bridge_config()
    except ConfigError as exc:
        print(f"error: {exc}")
        return 2
    for k, v in config.redacted().items():
        log.info("config %s = %s", k, v)
    bridge = Bridge(
        config,
        SlackClient(config.bot_token),
        EngineClient(config.engine_url, config.engine_token),
    )
    client = SocketModeClient(config.app_token, bridge.handle)
    stop = threading.Event()
    try:
        client.serve(stop)
    except SlackError as exc:
        print(f"error: {exc}")
        return 2
    except KeyboardInterrupt:
        stop.set()
    return 0

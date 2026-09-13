"""Zoom front end: the agent sits in the meeting, hears it, and answers.

A hardware requirement is usually spoken before it is ever typed. ``meetings/``
reads a Google Meet transcript *after* the call; this package goes one step
further and works while the call is happening -- it ingests Zoom's Realtime
Media Streams transcript, extracts board requests from what was actually said,
runs the pipeline, and speaks the answer back into the room.

What this package does **not** do, stated here rather than discovered later:

* **It does not perform the Zoom OAuth dance.** The host supplies the
  server-to-server credentials and the webhook secret token through the
  environment; acquiring, refreshing and storing them belongs to the host, and
  a package that mints its own credentials is a package that has to be trusted
  with them. See :mod:`zoombot.config`.
* **RTMS is receive-only.** Realtime Media Streams delivers audio and
  transcript *out* of a meeting; nothing in that API puts audio back in.
  Speaking out loud requires a real participant, which is the headless Meeting
  SDK container in ``zoombot/bot/``. The ``Speaker`` seam in
  :mod:`zoombot.speak` is what makes that statable instead of hidden:
  ``MeetingSdkSpeaker`` talks to that container, ``ChatSpeaker`` posts into the
  meeting chat over the REST API, and ``NullSpeaker`` records what would have
  been said. The runner names which one it used, so "the agent replied" can
  never be read as "the agent spoke out loud" when it did not.
* **It never orders anything.** The gates carried over from
  ``meetings/runner.py`` hold here: a request whose quote is not in the
  transcript is dropped, a low-confidence request is recorded but not built,
  and ``max_runs_per_meeting`` caps how many *paid* pipeline runs one meeting
  can start.

Modelled on Zoom's own open-source samples rather than on anything clever:
`zoom/rtms <https://github.com/zoom/rtms>`_ for the webhook
``endpoint.url_validation`` HMAC handshake and the signalling-then-media
WebSocket sequence, and `zoom/meetingsdk-headless-linux-sample
<https://github.com/zoom/meetingsdk-headless-linux-sample>`_ for the headless
participant that can actually emit audio.

**Unverified live.** This package has never been run against a real Zoom
account, not once. Every network boundary is a Protocol seam with a recorded
stand-in, so the whole test suite passes offline with no credentials -- which
proves the parsing and the gates, and proves nothing about Zoom's live
behaviour. The container in ``bot/`` is neither built nor run by the tests.

Structured like ``service/`` and ``slackbot/``: standard library only, no
engine logic of its own, and :mod:`zoombot.runner` is the only module that
knows both Zoom and silkscreen.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .config import (
    DEFAULT_API_BASE,
    DEFAULT_MAX_RUNS_PER_MEETING,
    DEFAULT_SPEAK_MODE,
    SPEAK_MODES,
    ZOOM_ENV,
    Config,
    ConfigError,
    load_config,
)

__all__ = [
    "Config",
    "ConfigError",
    "load_config",
    "ZOOM_ENV",
    "SPEAK_MODES",
    "DEFAULT_API_BASE",
    "DEFAULT_SPEAK_MODE",
    "DEFAULT_MAX_RUNS_PER_MEETING",
    # Resolved lazily; see __getattr__.
    "TranscriptChunk",
    "WebhookError",
    "verify_webhook",
    "url_validation_reply",
    "open_stream",
    "Speaker",
    "speaker_for",
    "ZoomReport",
    "run_meeting",
]

#: Which sibling module supplies each lazily-exported name. The imports are
#: deferred on purpose: ``import zoombot`` must keep working -- and
#: ``zoombot.config`` must stay importable by the service's integrations
#: report -- even when a sibling is missing or mid-edit. An eager
#: ``from .rtms import ...`` here would turn one broken module into a broken
#: package, and the failure would be reported against whoever imported it.
_LAZY = {
    "TranscriptChunk": "rtms",
    "WebhookError": "rtms",
    "verify_webhook": "rtms",
    "url_validation_reply": "rtms",
    "open_stream": "rtms",
    "Speaker": "speak",
    "speaker_for": "speak",
    "ZoomReport": "runner",
    "run_meeting": "runner",
}

if TYPE_CHECKING:  # pragma: no cover - for type checkers only.
    from .rtms import (  # noqa: F401
        TranscriptChunk,
        WebhookError,
        open_stream,
        url_validation_reply,
        verify_webhook,
    )
    from .runner import ZoomReport, run_meeting  # noqa: F401
    from .speak import Speaker, speaker_for  # noqa: F401


def __getattr__(name: str):
    """Import a sibling module only when one of its names is actually used."""
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(f".{module}", __name__), name)


def __dir__() -> list[str]:
    return sorted(__all__)

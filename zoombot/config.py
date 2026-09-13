"""Environment-variable configuration for the Zoom front end.

Everything this package needs is read once, here, and checked before anything
else runs. A half-configured integration is worse than one that refuses to
start: it comes up, looks healthy, and then dies on the first real webhook --
in the middle of a meeting, in front of the people who were counting on it.
So :func:`load_config` refuses to start, and it names **every** gap at once,
because a first-time setup should be one round of corrections rather than four.

Secrets arrive through the environment and leave through nothing.
:meth:`Config.redacted` exists so a startup banner can prove what was loaded
without printing any of it -- masks and lengths only, never a value and never a
tail. A tail is not a courtesy; for a webhook secret token it is most of the
secret.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

__all__ = [
    "Config",
    "ConfigError",
    "load_config",
    "ZOOM_ENV",
    "SPEAK_MODES",
    "DEFAULT_API_BASE",
    "DEFAULT_SPEAK_MODE",
    "DEFAULT_MAX_RUNS_PER_MEETING",
]

#: Every environment variable this package reads, in one place, so the docs,
#: the service's integrations report and ``.env.example`` can be checked
#: against a single list rather than against a grep.
ZOOM_ENV = (
    "ZOOM_CLIENT_ID",
    "ZOOM_CLIENT_SECRET",
    "ZOOM_ACCOUNT_ID",
    "ZOOM_WEBHOOK_SECRET_TOKEN",
    "ZOOM_RTMS_ENABLED",
    "ZOOM_SPEAK_MODE",
    "ZOOM_MEETINGS",
    "ZOOM_MAX_RUNS_PER_MEETING",
    "ZOOM_API_BASE",
    "ZOOM_PORT",
)

#: The four that have no sensible default. Ordered as ``ZOOM_ENV`` orders them
#: so an error message reads in the same order as the documentation.
REQUIRED_ENV = (
    "ZOOM_CLIENT_ID",
    "ZOOM_CLIENT_SECRET",
    "ZOOM_ACCOUNT_ID",
    "ZOOM_WEBHOOK_SECRET_TOKEN",
)

#: Zoom REST API v2. Pinned to a major version rather than left "latest", for
#: the same reason ``meetings/config.py`` pins Meet v2: a silent major-version
#: change would reshape the responses under a parser that has no way to notice,
#: and the first symptom would be a wrong answer rather than an exception.
DEFAULT_API_BASE = "https://api.zoom.us/v2"

#: How the agent talks back. ``sdk`` is the headless Meeting SDK participant in
#: ``zoombot/bot/`` (real audio in the room); ``chat`` posts into the meeting
#: chat over the REST API; ``off`` records what would have been said and says
#: nothing. There is no fourth value and no silent fallback -- an agent that
#: quietly stops speaking is exactly the failure this package must not have.
SPEAK_MODES = ("sdk", "chat", "off")

DEFAULT_SPEAK_MODE = "chat"

#: Each run is a *paid* pipeline run. One meeting that says "board" nine times
#: must not bill for nine boards, so the cap is small and always present.
DEFAULT_MAX_RUNS_PER_MEETING = 2

#: Default listen port for ``python -m zoombot``. Not 8080 or 8081: both are
#: already taken on this team's machines (see CLAUDE.md).
DEFAULT_PORT = 8095

#: An api_base must end in a version segment: ``/v2``, ``/v3``, ... The point is
#: not the number, it is that some number is stated.
_VERSION_SEGMENT = re.compile(r"/v\d+$")

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


class ConfigError(RuntimeError):
    """The Zoom integration is not configured well enough to start.

    ``errors`` carries every problem found, not just the first one. The
    message is those problems joined, so a caller that only prints the
    exception still sees all of them.
    """

    def __init__(self, problems: str | list[str] | tuple[str, ...]) -> None:
        if isinstance(problems, str):
            problems = [problems]
        self.errors: tuple[str, ...] = tuple(problems)
        joined = "; ".join(self.errors)
        super().__init__(
            f"zoombot is not configured: {joined}. "
            f"See docs/zoom.md and .env.example for every variable in ZOOM_ENV."
        )


@dataclass(frozen=True)
class Config:
    """What the Zoom front end needs, resolved once and validated once.

    The credentials are a Zoom server-to-server OAuth app's. This package
    deliberately does **not** perform the OAuth dance: acquiring, refreshing and
    storing a token belongs to the host, and a package that mints its own
    credentials is a package that has to be trusted with them.
    """

    client_id: str
    client_secret: str
    account_id: str
    webhook_secret: str
    api_base: str = DEFAULT_API_BASE
    #: ``"sdk"``, ``"chat"`` or ``"off"``; see :data:`SPEAK_MODES`.
    speak_mode: str = DEFAULT_SPEAK_MODE
    #: Only act on these meeting ids. **Empty means every meeting these
    #: credentials can see**, which on an account-level app is the whole
    #: account -- rarely what anyone wants, and stated here so nobody has to
    #: infer it from an empty tuple.
    meeting_allowlist: tuple[str, ...] = ()
    #: Hard cap on board runs started from one meeting. Each one is a paid
    #: pipeline run, so a non-positive value is an error rather than a
    #: helpfully-interpreted "unlimited".
    max_runs_per_meeting: int = DEFAULT_MAX_RUNS_PER_MEETING
    #: Whether to ingest Realtime Media Streams. RTMS is receive-only; turning
    #: it off leaves a bot that can still be addressed but hears nothing.
    rtms_enabled: bool = True
    #: The port ``python -m zoombot`` listens on. Declared rather than left to
    #: a fallback in ``app.py``: the webhook address is registered in the Zoom
    #: dashboard, so which port this binds is deployment configuration, not an
    #: implementation detail. 8080 and 8081 are already taken on this team's
    #: machines, which is why the default is not either of them.
    port: int = DEFAULT_PORT

    def __post_init__(self) -> None:
        problems = _problems(
            client_id=self.client_id,
            client_secret=self.client_secret,
            account_id=self.account_id,
            webhook_secret=self.webhook_secret,
            api_base=self.api_base,
            speak_mode=self.speak_mode,
            max_runs_per_meeting=self.max_runs_per_meeting,
            port=self.port,
            label=_field_label,
        )
        if problems:
            raise ConfigError(problems)

    def allows(self, meeting_id: str) -> bool:
        """Is this meeting in scope? An empty allowlist allows everything."""
        if not self.meeting_allowlist:
            return True
        return str(meeting_id).strip() in self.meeting_allowlist

    def redacted(self) -> dict[str, str]:
        """A printable view: masks and lengths, never a value, never a tail."""
        return {
            "client_id": _mask(self.client_id),
            "client_secret": _mask(self.client_secret),
            "account_id": _mask(self.account_id),
            "webhook_secret": _mask(self.webhook_secret),
            "api_base": self.api_base,
            "speak_mode": self.speak_mode,
            "rtms_enabled": "yes" if self.rtms_enabled else "no",
            "meeting_allowlist": (
                ", ".join(self.meeting_allowlist)
                or "<any meeting these credentials see>"
            ),
            "max_runs_per_meeting": str(self.max_runs_per_meeting),
            "port": str(self.port),
        }


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Build a :class:`Config` from the environment.

    Raises:
        ConfigError: naming every problem at once -- each missing variable, a
            bad ``ZOOM_SPEAK_MODE``, a plaintext or unversioned ``ZOOM_API_BASE``
            and an unusable ``ZOOM_MAX_RUNS_PER_MEETING`` all appear in the same
            message rather than one per restart.
    """
    env = dict(os.environ if env is None else env)
    problems: list[str] = []

    def _text(key: str) -> str:
        return (env.get(key) or "").strip()

    api_base = _text("ZOOM_API_BASE") or DEFAULT_API_BASE
    speak_mode = _text("ZOOM_SPEAK_MODE").lower() or DEFAULT_SPEAK_MODE

    raw_runs = _text("ZOOM_MAX_RUNS_PER_MEETING")
    max_runs = DEFAULT_MAX_RUNS_PER_MEETING
    if raw_runs:
        try:
            max_runs = int(raw_runs)
        except ValueError:
            problems.append(
                f"ZOOM_MAX_RUNS_PER_MEETING must be a whole number, got {raw_runs!r}"
            )
            # Keep the default so the remaining checks still run and report.
            max_runs = DEFAULT_MAX_RUNS_PER_MEETING

    raw_rtms = _text("ZOOM_RTMS_ENABLED").lower()
    rtms_enabled = True
    if raw_rtms:
        if raw_rtms in _TRUE:
            rtms_enabled = True
        elif raw_rtms in _FALSE:
            rtms_enabled = False
        else:
            problems.append(
                f"ZOOM_RTMS_ENABLED must be one of {', '.join(_TRUE + _FALSE)}, "
                f"got {raw_rtms!r}"
            )

    raw_port = _text("ZOOM_PORT")
    port = DEFAULT_PORT
    if raw_port:
        try:
            port = int(raw_port)
        except ValueError:
            problems.append(f"ZOOM_PORT must be a whole number, got {raw_port!r}")
            port = DEFAULT_PORT

    allowlist = tuple(
        part.strip() for part in _text("ZOOM_MEETINGS").split(",") if part.strip()
    )

    problems.extend(
        _problems(
            client_id=_text("ZOOM_CLIENT_ID"),
            client_secret=_text("ZOOM_CLIENT_SECRET"),
            account_id=_text("ZOOM_ACCOUNT_ID"),
            webhook_secret=_text("ZOOM_WEBHOOK_SECRET_TOKEN"),
            api_base=api_base,
            speak_mode=speak_mode,
            max_runs_per_meeting=max_runs,
            port=port,
            label=_env_label,
        )
    )
    if problems:
        raise ConfigError(problems)

    return Config(
        client_id=_text("ZOOM_CLIENT_ID"),
        client_secret=_text("ZOOM_CLIENT_SECRET"),
        account_id=_text("ZOOM_ACCOUNT_ID"),
        webhook_secret=_text("ZOOM_WEBHOOK_SECRET_TOKEN"),
        api_base=api_base,
        speak_mode=speak_mode,
        meeting_allowlist=allowlist,
        max_runs_per_meeting=max_runs,
        rtms_enabled=rtms_enabled,
        port=port,
    )


_FIELD_TO_ENV = {
    "client_id": "ZOOM_CLIENT_ID",
    "client_secret": "ZOOM_CLIENT_SECRET",
    "account_id": "ZOOM_ACCOUNT_ID",
    "webhook_secret": "ZOOM_WEBHOOK_SECRET_TOKEN",
    "api_base": "ZOOM_API_BASE",
    "speak_mode": "ZOOM_SPEAK_MODE",
    "max_runs_per_meeting": "ZOOM_MAX_RUNS_PER_MEETING",
    "port": "ZOOM_PORT",
}


def _env_label(field: str) -> str:
    return _FIELD_TO_ENV[field]


def _field_label(field: str) -> str:
    return field


def _problems(
    *,
    client_id: str,
    client_secret: str,
    account_id: str,
    webhook_secret: str,
    api_base: str,
    speak_mode: str,
    max_runs_per_meeting: int,
    port: int = DEFAULT_PORT,
    label,
) -> list[str]:
    """Every problem with these values, in one list.

    Shared by :meth:`Config.__post_init__` and :func:`load_config` so that
    constructing a Config directly and loading one from the environment cannot
    disagree about what counts as valid. ``label`` decides whether a problem is
    named after the field or after the environment variable that feeds it.
    """
    problems: list[str] = []

    for field, value in (
        ("client_id", client_id),
        ("client_secret", client_secret),
        ("account_id", account_id),
        ("webhook_secret", webhook_secret),
    ):
        if not str(value or "").strip():
            problems.append(f"{label(field)} is not set")

    base = str(api_base or "").strip()
    if not base.startswith("https://"):
        problems.append(
            f"{label('api_base')} must be https, got {base!r} -- an OAuth token "
            f"must never travel over plaintext"
        )
    if not _VERSION_SEGMENT.search(base.rstrip("/")):
        problems.append(
            f"{label('api_base')} must be pinned to a major version ending in "
            f"/v<N> (default {DEFAULT_API_BASE}), got {base!r} -- an unpinned "
            f"base lets a major-version change reshape responses silently"
        )

    if speak_mode not in SPEAK_MODES:
        problems.append(
            f"{label('speak_mode')} must be one of {', '.join(SPEAK_MODES)}, got "
            f"{str(speak_mode)!r} -- there is no fallback to 'off', because an "
            f"agent that silently stops speaking looks like an agent that is "
            f"working"
        )

    if not isinstance(max_runs_per_meeting, int) or isinstance(
        max_runs_per_meeting, bool
    ):
        problems.append(
            f"{label('max_runs_per_meeting')} must be a whole number, got "
            f"{max_runs_per_meeting!r}"
        )
    elif max_runs_per_meeting < 1:
        problems.append(
            f"{label('max_runs_per_meeting')} must be at least 1, got "
            f"{max_runs_per_meeting} -- each run is a paid pipeline run, so "
            f"zero or negative is a typo, not 'unlimited'"
        )

    if not isinstance(port, int) or isinstance(port, bool):
        problems.append(f"{label('port')} must be a whole number, got {port!r}")
    elif not 1 <= port <= 65535:
        problems.append(
            f"{label('port')} must be between 1 and 65535, got {port} -- the "
            f"webhook address registered with Zoom points at this port, so a "
            f"value that cannot be bound is a deployment that never receives "
            f"an event"
        )

    return problems


def _mask(secret: str) -> str:
    """``<set, N chars>`` or ``<unset>``. No prefix, no tail, no hash."""
    text = str(secret or "")
    return f"<set, {len(text)} chars>" if text else "<unset>"

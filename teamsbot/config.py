"""Environment-variable configuration for the Microsoft Teams front end.

Everything this package needs is read once, here, and checked before anything
else runs. A half-configured integration is worse than one that refuses to
start: it comes up, looks healthy, and then dies on the first real callback --
in the middle of a meeting, in front of the people who were counting on it. So
:func:`load_config` refuses to start, and it names **every** gap at once,
because a first-time setup should be one round of corrections rather than four.

The credentials are an Entra ID (Azure AD) application registration's:
``TEAMS_APP_ID`` / ``TEAMS_APP_SECRET`` for the app itself and
``TEAMS_TENANT_ID`` for the directory it is installed in. This package
deliberately does **not** perform an interactive OAuth dance -- there is no
browser consent flow here, no token file, no refresh-token storage. The host
(or the app registration's own client-credentials grant, which
:mod:`teamsbot.graph` performs against ``login.microsoftonline.com``) supplies
the credentials; a package that mints and stores its own user credentials is a
package that has to be trusted with them.

Secrets arrive through the environment and leave through nothing.
:meth:`Config.redacted` exists so a startup banner can prove what was loaded
without printing any of it -- masks and lengths only, never a value and never a
tail. A tail is not a courtesy; for a client secret it is most of the secret.

Basis: the app-registration and bot-endpoint shape of
``microsoft/BotBuilder-Samples`` (a calling bot is an Entra app with a public
``/api/calls`` endpoint), and the documented Microsoft Graph REST surface for
online meetings and call transcripts. **None of this has been run against a
live tenant from this repo.**
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
    "TEAMS_ENV",
    "REQUIRED_ENV",
    "SPEAK_MODES",
    "DEFAULT_GRAPH_BASE",
    "DEFAULT_SPEAK_MODE",
    "DEFAULT_MAX_RUNS_PER_MEETING",
    "DEFAULT_PORT",
]

#: Every environment variable this package reads, in one place, so the docs,
#: the service's integrations report and ``.env.example`` can be checked
#: against a single list rather than against a grep.
TEAMS_ENV = (
    "TEAMS_APP_ID",
    "TEAMS_APP_SECRET",
    "TEAMS_TENANT_ID",
    "TEAMS_BOT_ENDPOINT",
    "TEAMS_SPEAK_MODE",
    "TEAMS_MEETINGS",
    "TEAMS_MAX_RUNS_PER_MEETING",
    "TEAMS_GRAPH_BASE",
    "TEAMS_PORT",
)

#: The three that have no sensible default. Ordered as :data:`TEAMS_ENV` orders
#: them so an error message reads in the same order as the documentation.
REQUIRED_ENV = (
    "TEAMS_APP_ID",
    "TEAMS_APP_SECRET",
    "TEAMS_TENANT_ID",
)

#: Microsoft Graph v1.0. Pinned to a version rather than left "latest", for the
#: same reason ``meetings/config.py`` pins Meet v2: a silent version change
#: would reshape the responses under a parser that has no way to notice, and
#: the first symptom would be a wrong answer rather than an exception. Graph's
#: ``/beta`` is explicitly *not* the default -- it is documented as subject to
#: change without notice.
DEFAULT_GRAPH_BASE = "https://graph.microsoft.com/v1.0"

#: How the agent talks back. ``sdk`` is the policy-gated Graph calling bot in
#: ``teamsbot/bot/`` (real audio in the meeting, and the half that needs
#: ``TEAMS_BOT_ENDPOINT``); ``chat`` posts into the meeting chat via
#: ``/chats/{id}/messages``; ``off`` records what would have been said and says
#: nothing. There is no fourth value and no silent fallback -- an agent that
#: quietly stops speaking looks exactly like an agent that is working.
SPEAK_MODES = ("sdk", "chat", "off")

DEFAULT_SPEAK_MODE = "chat"

#: Each run is a *paid* pipeline run. One meeting that says "board" nine times
#: must not bill for nine boards, so the cap is small and always present.
DEFAULT_MAX_RUNS_PER_MEETING = 2

#: Default listen port for ``python -m teamsbot``. 3978 is the Bot Framework
#: convention every sample and ngrok recipe uses, which is the only reason it
#: is this number rather than a free one.
DEFAULT_PORT = 3978

#: A graph_base must end in a version segment: ``/v1.0``, ``/beta``, ... The
#: point is not the number, it is that some version is stated.
_VERSION_SEGMENT = re.compile(r"/(v\d+(\.\d+)?|beta)$")


class ConfigError(RuntimeError):
    """The Teams integration is not configured well enough to start.

    ``errors`` carries every problem found, not just the first one. The message
    is those problems joined, so a caller that only prints the exception still
    sees all of them.
    """

    def __init__(self, problems: str | list[str] | tuple[str, ...]) -> None:
        if isinstance(problems, str):
            problems = [problems]
        self.errors: tuple[str, ...] = tuple(problems)
        joined = "; ".join(self.errors)
        super().__init__(
            f"teamsbot is not configured: {joined}. "
            f"See docs/teams.md and .env.example for every variable in TEAMS_ENV."
        )


@dataclass(frozen=True)
class Config:
    """What the Teams front end needs, resolved once and validated once."""

    app_id: str
    app_secret: str
    tenant_id: str
    #: The public https URL Teams calls back on. Needed only by the calling
    #: bot, so it is optional in general and **required when**
    #: ``speak_mode == "sdk"``: a calling bot with nowhere to be called is not
    #: a configuration this package will pretend works.
    bot_endpoint: str = ""
    graph_base: str = DEFAULT_GRAPH_BASE
    #: ``"sdk"``, ``"chat"`` or ``"off"``; see :data:`SPEAK_MODES`.
    speak_mode: str = DEFAULT_SPEAK_MODE
    #: Only act on these meeting ids. **Empty means every meeting these
    #: credentials can see**, which with application permissions is the whole
    #: tenant -- rarely what anyone wants, and stated here so nobody has to
    #: infer it from an empty tuple.
    meeting_allowlist: tuple[str, ...] = ()
    #: Hard cap on board runs started from one meeting. Each one is a paid
    #: pipeline run, so a non-positive value is an error rather than a
    #: helpfully-interpreted "unlimited".
    max_runs_per_meeting: int = DEFAULT_MAX_RUNS_PER_MEETING
    #: The port ``python -m teamsbot`` listens on. Declared rather than left to
    #: a default in ``app.py``: the callback address is registered with
    #: Microsoft and points at a port, so which port this binds is deployment
    #: configuration, not an implementation detail.
    port: int = DEFAULT_PORT

    def __post_init__(self) -> None:
        problems = _problems(
            app_id=self.app_id,
            app_secret=self.app_secret,
            tenant_id=self.tenant_id,
            bot_endpoint=self.bot_endpoint,
            graph_base=self.graph_base,
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

    def token_url(self) -> str:
        """The client-credentials token endpoint for this tenant.

        Lives here rather than in :mod:`teamsbot.graph` because the tenant id
        is configuration, and because one definition means the allowlist check
        in ``graph.py`` and the URL it checks cannot drift apart.
        """
        return (
            f"https://login.microsoftonline.com/{self.tenant_id}"
            f"/oauth2/v2.0/token"
        )

    def redacted(self) -> dict[str, str]:
        """A printable view: masks and lengths, never a value, never a tail.

        ``app_id`` and ``tenant_id`` are GUIDs that identify the app rather
        than authenticate it, but they are masked too: this dict exists to
        prove settings are *present*, and a redaction rule with exceptions is a
        redaction rule someone will get wrong later.
        """
        return {
            "app_id": _mask(self.app_id),
            "app_secret": _mask(self.app_secret),
            "tenant_id": _mask(self.tenant_id),
            "bot_endpoint": _mask(self.bot_endpoint),
            "graph_base": self.graph_base,
            "speak_mode": self.speak_mode,
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
            bad ``TEAMS_SPEAK_MODE``, a plaintext or unversioned
            ``TEAMS_GRAPH_BASE``, a missing ``TEAMS_BOT_ENDPOINT`` under
            ``speak_mode=sdk``, an unusable ``TEAMS_MAX_RUNS_PER_MEETING`` and
            an unbindable ``TEAMS_PORT`` all appear in the same message rather
            than one per restart.
    """
    env = dict(os.environ if env is None else env)
    problems: list[str] = []

    def _text(key: str) -> str:
        return (env.get(key) or "").strip()

    graph_base = _text("TEAMS_GRAPH_BASE") or DEFAULT_GRAPH_BASE
    speak_mode = _text("TEAMS_SPEAK_MODE").lower() or DEFAULT_SPEAK_MODE

    raw_runs = _text("TEAMS_MAX_RUNS_PER_MEETING")
    max_runs = DEFAULT_MAX_RUNS_PER_MEETING
    if raw_runs:
        try:
            max_runs = int(raw_runs)
        except ValueError:
            problems.append(
                f"TEAMS_MAX_RUNS_PER_MEETING must be a whole number, got {raw_runs!r}"
            )
            # Keep the default so the remaining checks still run and report.
            max_runs = DEFAULT_MAX_RUNS_PER_MEETING

    raw_port = _text("TEAMS_PORT")
    port = DEFAULT_PORT
    if raw_port:
        try:
            port = int(raw_port)
        except ValueError:
            problems.append(f"TEAMS_PORT must be a whole number, got {raw_port!r}")
            port = DEFAULT_PORT

    allowlist = tuple(
        part.strip() for part in _text("TEAMS_MEETINGS").split(",") if part.strip()
    )

    problems.extend(
        _problems(
            app_id=_text("TEAMS_APP_ID"),
            app_secret=_text("TEAMS_APP_SECRET"),
            tenant_id=_text("TEAMS_TENANT_ID"),
            bot_endpoint=_text("TEAMS_BOT_ENDPOINT"),
            graph_base=graph_base,
            speak_mode=speak_mode,
            max_runs_per_meeting=max_runs,
            port=port,
            label=_env_label,
        )
    )
    if problems:
        raise ConfigError(problems)

    return Config(
        app_id=_text("TEAMS_APP_ID"),
        app_secret=_text("TEAMS_APP_SECRET"),
        tenant_id=_text("TEAMS_TENANT_ID"),
        bot_endpoint=_text("TEAMS_BOT_ENDPOINT"),
        graph_base=graph_base,
        speak_mode=speak_mode,
        meeting_allowlist=allowlist,
        max_runs_per_meeting=max_runs,
        port=port,
    )


_FIELD_TO_ENV = {
    "app_id": "TEAMS_APP_ID",
    "app_secret": "TEAMS_APP_SECRET",
    "tenant_id": "TEAMS_TENANT_ID",
    "bot_endpoint": "TEAMS_BOT_ENDPOINT",
    "graph_base": "TEAMS_GRAPH_BASE",
    "speak_mode": "TEAMS_SPEAK_MODE",
    "max_runs_per_meeting": "TEAMS_MAX_RUNS_PER_MEETING",
    "port": "TEAMS_PORT",
}


def _env_label(field: str) -> str:
    return _FIELD_TO_ENV[field]


def _field_label(field: str) -> str:
    return field


def _problems(
    *,
    app_id: str,
    app_secret: str,
    tenant_id: str,
    bot_endpoint: str,
    graph_base: str,
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
        ("app_id", app_id),
        ("app_secret", app_secret),
        ("tenant_id", tenant_id),
    ):
        if not str(value or "").strip():
            problems.append(f"{label(field)} is not set")

    base = str(graph_base or "").strip()
    if not base.startswith("https://"):
        problems.append(
            f"{label('graph_base')} must be https, got {base!r} -- a bearer "
            f"token must never travel over plaintext"
        )
    if not _VERSION_SEGMENT.search(base.rstrip("/")):
        problems.append(
            f"{label('graph_base')} must be pinned to a version ending in "
            f"/v<N> or /beta (default {DEFAULT_GRAPH_BASE}), got {base!r} -- an "
            f"unpinned base lets a version change reshape responses silently"
        )

    if speak_mode not in SPEAK_MODES:
        problems.append(
            f"{label('speak_mode')} must be one of {', '.join(SPEAK_MODES)}, got "
            f"{str(speak_mode)!r} -- there is no fallback to 'off', because an "
            f"agent that silently stops speaking looks like an agent that is "
            f"working"
        )

    endpoint = str(bot_endpoint or "").strip()
    if endpoint and not endpoint.startswith("https://"):
        problems.append(
            f"{label('bot_endpoint')} must be https, got {endpoint!r} -- Teams "
            f"will not call a plaintext endpoint and neither will this package"
        )
    if speak_mode == "sdk" and not endpoint:
        problems.append(
            f"{label('bot_endpoint')} is not set, but "
            f"{label('speak_mode')}=sdk needs a public https callback URL for "
            f"the calling bot -- without one the bot can never be reached"
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
            f"callback address registered with Microsoft points at this port, "
            f"so a value that cannot be bound is a deployment that never "
            f"receives a notification"
        )

    return problems


def _mask(secret: str) -> str:
    """``<set, N chars>`` or ``<unset>``. No prefix, no tail, no hash."""
    text = str(secret or "")
    return f"<set, {len(text)} chars>" if text else "<unset>"

"""Environment-variable configuration for the Slack bot.

Every secret arrives through the environment. Nothing is read from a file that
could be committed, and no value is ever echoed back into Slack or a log line --
:meth:`Config.redacted` exists so a startup banner can prove what was loaded
without printing it.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

from silkscreen.agents.model import DEFAULT_MODEL  # noqa: E402
from silkscreen.cli import _load_dotenv  # noqa: E402

__all__ = ["Config", "ConfigError", "load_config"]

#: Slack requires an HTTP 200 within three seconds. Every run is far slower
#: than that, so the handler acknowledges immediately and works in a thread.
DEFAULT_PORT = 3000
DEFAULT_TIME_LIMIT_S = 20.0
DEFAULT_MAX_CONCURRENT_RUNS = 2


class ConfigError(RuntimeError):
    """A required setting is missing or malformed."""


@dataclass(frozen=True)
class Config:
    """Everything the bot needs to run, resolved once at startup."""

    bot_token: str
    signing_secret: str
    port: int = DEFAULT_PORT
    model: str = DEFAULT_MODEL
    time_limit_s: float = DEFAULT_TIME_LIMIT_S
    max_repairs: int = 3
    max_concurrent_runs: int = DEFAULT_MAX_CONCURRENT_RUNS
    #: When non-empty, the bot answers only in these channel IDs. A workspace
    #: admin can install the app broadly and still keep runs -- which cost
    #: model calls -- confined to the channels that are paying for them.
    allowed_channels: frozenset[str] = field(default_factory=frozenset)
    #: Directory for the artifacts of each run. Files are uploaded to Slack and
    #: the local copy is what a human inspects afterwards in KiCad.
    workdir: Path = field(default_factory=lambda: Path("slack-runs"))

    def channel_allowed(self, channel_id: str) -> bool:
        return not self.allowed_channels or channel_id in self.allowed_channels

    def redacted(self) -> dict[str, str]:
        """A printable view. Secrets become a length, never a prefix."""
        return {
            "bot_token": f"<set, {len(self.bot_token)} chars>",
            "signing_secret": f"<set, {len(self.signing_secret)} chars>",
            "port": str(self.port),
            "model": self.model,
            "time_limit_s": str(self.time_limit_s),
            "max_concurrent_runs": str(self.max_concurrent_runs),
            "allowed_channels": ", ".join(sorted(self.allowed_channels)) or "<any>",
            "workdir": str(self.workdir),
        }


def _int(env: dict[str, str], key: str, default: int) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc


def _float(env: dict[str, str], key: str, default: float) -> float:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number, got {raw!r}") from exc


def load_config(env: dict[str, str] | None = None, *, dotenv: bool = True) -> Config:
    """Build a :class:`Config` from the environment.

    Raises:
        ConfigError: naming every missing setting at once, so a first-time
            setup is one round of corrections rather than three.
    """
    if env is None:
        if dotenv:
            _load_dotenv(Path.cwd() / ".env")
        env = dict(os.environ)

    missing = [
        name
        for name in ("SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET")
        if not env.get(name, "").strip()
    ]
    # The pipeline itself needs a key; without one every run fails at the first
    # model call, which is a worse way to learn than refusing to start.
    if not env.get("GOOGLE_API_KEY", "").strip():
        missing.append("GOOGLE_API_KEY")
    if missing:
        raise ConfigError(
            "missing required environment variable(s): "
            + ", ".join(missing)
            + ". See .env.example and the Slack setup section of the README."
        )

    channels = frozenset(
        part.strip()
        for part in env.get("SILKSCREEN_SLACK_CHANNELS", "").split(",")
        if part.strip()
    )
    workdir = env.get("SILKSCREEN_SLACK_WORKDIR", "").strip() or "slack-runs"

    return Config(
        bot_token=env["SLACK_BOT_TOKEN"].strip(),
        signing_secret=env["SLACK_SIGNING_SECRET"].strip(),
        port=_int(env, "SILKSCREEN_SLACK_PORT", DEFAULT_PORT),
        model=env.get("SILKSCREEN_MODEL", "").strip() or DEFAULT_MODEL,
        time_limit_s=_float(env, "SILKSCREEN_SLACK_TIME_LIMIT", DEFAULT_TIME_LIMIT_S),
        max_repairs=_int(env, "SILKSCREEN_SLACK_REPAIRS", 3),
        max_concurrent_runs=_int(
            env, "SILKSCREEN_SLACK_MAX_RUNS", DEFAULT_MAX_CONCURRENT_RUNS
        ),
        allowed_channels=channels,
        workdir=Path(workdir),
    )

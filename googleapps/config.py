"""Environment-variable configuration for the Google Workspace commands.

Every secret arrives through the environment. Nothing is read from a file
that could be committed, no credential is ever embedded in code, and no value
is ever echoed to the terminal or a log line -- :meth:`Config.redacted`
exists so ``check`` can prove what was loaded without printing it.

Unlike the Slack-style always-on services, the three destinations here are
independent: posting a Chat card needs only the webhook, Gmail and Calendar
need the OAuth client, and a plain ``run`` needs only ``GOOGLE_API_KEY``. So
nothing is required at load time; each command calls the ``require_*`` method
for exactly what it is about to use, and gets every missing name at once.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

from silkscreen.agents.model import DEFAULT_MODEL  # noqa: E402
from silkscreen.cli import _load_dotenv  # noqa: E402

from .transport import describe_secret, mask  # noqa: E402

__all__ = ["Config", "ConfigError", "load_config", "DEFAULT_TOKEN_PATH"]

DEFAULT_TOKEN_PATH = Path.home() / ".config" / "silkscreen" / "google-token.json"

SETUP_HINT = "See docs/googleapps.md for the setup walkthrough."


class ConfigError(RuntimeError):
    """A required setting is missing or malformed."""


@dataclass(frozen=True)
class Config:
    """Everything the commands can use, resolved once at startup.

    Empty string means unset; the ``require_*`` methods are the gate.
    """

    client_id: str = ""
    client_secret: str = ""
    chat_webhook: str = ""
    token_path: Path = DEFAULT_TOKEN_PATH
    google_api_key: str = ""
    model: str = DEFAULT_MODEL

    def require_oauth(self) -> None:
        """Gmail and Calendar need the OAuth client. Names every gap at once."""
        missing = [
            name
            for name, value in (
                ("GOOGLEAPPS_CLIENT_ID", self.client_id),
                ("GOOGLEAPPS_CLIENT_SECRET", self.client_secret),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                "missing required environment variable(s): "
                + ", ".join(missing)
                + f". Create a Desktop-app OAuth client first. {SETUP_HINT}"
            )

    def require_webhook(self) -> None:
        if not self.chat_webhook:
            raise ConfigError(
                "missing required environment variable: GOOGLEAPPS_CHAT_WEBHOOK. "
                f"Create an incoming webhook in the Chat space. {SETUP_HINT}"
            )

    def require_api_key(self) -> None:
        """The pipeline itself needs a key; without one every run fails at the
        first model call, which is a worse way to learn than refusing to start."""
        if not self.google_api_key:
            raise ConfigError(
                "missing required environment variable: GOOGLE_API_KEY. "
                "The pipeline cannot call Gemini without it."
            )

    def redacted(self) -> dict[str, str]:
        """A printable view. Secrets become a tail or a length, never a value."""
        return {
            "client_id": mask(self.client_id, 8) if self.client_id else "<unset>",
            "client_secret": (
                f"<set, {len(self.client_secret)} chars>"
                if self.client_secret
                else "<unset>"
            ),
            # The URL's tail *is* the token, so not even a tail is shown.
            "chat_webhook": describe_secret(self.chat_webhook),
            "token_path": str(self.token_path),
            "google_api_key": (
                f"<set, {len(self.google_api_key)} chars>"
                if self.google_api_key
                else "<unset>"
            ),
            "model": self.model,
        }


def load_config(env: dict[str, str] | None = None, *, dotenv: bool = True) -> Config:
    """Build a :class:`Config` from the environment.

    Reads ``.env`` from the working directory first, exactly the way the CLI
    does (and via the same loader), so a repo-root ``.env`` serves both.
    """
    if env is None:
        if dotenv:
            _load_dotenv(Path.cwd() / ".env")
        env = dict(os.environ)

    token_path = env.get("GOOGLEAPPS_TOKEN_PATH", "").strip()
    return Config(
        client_id=env.get("GOOGLEAPPS_CLIENT_ID", "").strip(),
        client_secret=env.get("GOOGLEAPPS_CLIENT_SECRET", "").strip(),
        chat_webhook=env.get("GOOGLEAPPS_CHAT_WEBHOOK", "").strip(),
        token_path=Path(token_path).expanduser() if token_path else DEFAULT_TOKEN_PATH,
        google_api_key=env.get("GOOGLE_API_KEY", "").strip(),
        model=env.get("SILKSCREEN_MODEL", "").strip() or DEFAULT_MODEL,
    )

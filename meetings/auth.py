"""Getting a Meet token, instead of being handed one.

``MeetConfig`` says this package "deliberately does not implement the OAuth
dance", and that was the right call for the *client*: a package that mints its
own credentials is a package that has to be trusted with them. But it left
``TODO.txt``'s "there is no OAuth acquisition or refresh -- the host supplies
MEET_ACCESS_TOKEN", which in practice meant nobody could run this at all
without hand-minting a token in the OAuth playground.

So acquisition lives here, one module away from the client, and it does not
invent any machinery: it is ``googleapps.auth``'s flow -- browser consent,
127.0.0.1 loopback, PKCE S256, refresh, 0600 token -- pointed at
:data:`~meetings.config.MEET_SCOPES` and a token file of its own.

Two things that are deliberate:

* **A separate token path.** Sharing ``google-token.json`` with Gmail and
  Calendar would mean the last flow to run silently narrows the other's
  scopes: Google returns a token for the scopes *just consented*, so a Meet
  sign-in would overwrite a Gmail token with one that cannot send mail, and
  the failure would surface much later as a 403 on an unrelated feature.
* **Read-only scopes, and only Meet's.** Nothing here creates, modifies or
  joins a meeting. Asking for write access an integration does not use is how
  it gets refused by the admin reading the consent screen.

What this does **not** solve: Meet only exposes a transcript when the
organiser enabled transcription, which is a Google Workspace feature. A
consumer account can complete every step in this file and still have nothing
to read, because no transcript will ever exist.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from googleapps.auth import AuthError, access_token, run_auth_flow, token_status
from googleapps.config import Config
from googleapps.transport import Transport, urllib_transport

from .config import MEET_SCOPES, ConfigError, MeetConfig

__all__ = [
    "DEFAULT_MEET_TOKEN_PATH",
    "meet_config_from_token",
    "meet_token",
    "meet_token_status",
    "run_meet_auth",
]

#: Meet's own token file. Not shared with Gmail/Calendar -- see the module
#: docstring; a shared file means one sign-in silently de-scopes the other.
DEFAULT_MEET_TOKEN_PATH = Path.home() / ".config" / "silkscreen" / "meet-token.json"


def _config(
    token_path: Path | None = None, env: dict[str, str] | None = None
) -> Config:
    """The OAuth client, from the same env vars the Gmail flow reads.

    The *client* is shared on purpose -- it is one Desktop-app OAuth client
    per project, and making Meet need a second one would be setup friction
    with no security gain. Only the token is separate.
    """
    src = dict(os.environ if env is None else env)
    config = Config(
        client_id=src.get("GOOGLEAPPS_CLIENT_ID", "").strip(),
        client_secret=src.get("GOOGLEAPPS_CLIENT_SECRET", "").strip(),
        token_path=token_path or DEFAULT_MEET_TOKEN_PATH,
    )
    config.require_oauth()
    return config


def run_meet_auth(
    *,
    token_path: Path | None = None,
    env: dict[str, str] | None = None,
    transport: Transport = urllib_transport,
    open_browser: Callable[[str], Any] | None = None,
    on_url: Callable[[str], None] | None = None,
    authorize: Callable[[Callable[[str], str], str], str] | None = None,
) -> Path:
    """Open Google's consent page for Meet and store the token. Returns the path."""
    return run_auth_flow(
        _config(token_path, env),
        transport,
        authorize=authorize,
        open_browser=open_browser,
        on_url=on_url,
        scopes=MEET_SCOPES,
    )


def meet_token(
    *,
    token_path: Path | None = None,
    env: dict[str, str] | None = None,
    transport: Transport = urllib_transport,
) -> str:
    """A currently-valid Meet access token, refreshing if needed.

    ``MEET_ACCESS_TOKEN`` still wins when set, so a host that already has its
    own credential machinery keeps working unchanged -- this is an added path,
    not a replacement for the seam ``MeetConfig`` documents.
    """
    src = dict(os.environ if env is None else env)
    supplied = src.get("MEET_ACCESS_TOKEN", "").strip()
    if supplied:
        return supplied
    return access_token(_config(token_path, env), transport)


def meet_token_status(token_path: Path | None = None) -> str:
    """``missing`` / ``expired`` / ``valid`` -- local only, for a check command."""
    return token_status(token_path or DEFAULT_MEET_TOKEN_PATH)


def meet_config_from_token(
    *,
    token_path: Path | None = None,
    env: dict[str, str] | None = None,
    transport: Transport = urllib_transport,
    **kwargs: Any,
) -> MeetConfig:
    """A ready :class:`MeetConfig`, or a refusal naming the one fix.

    The refusal matters: a missing token used to surface as an HTTP 401 from
    the first Meet call, which reads as "the API rejected us" rather than
    "nobody has signed in yet".
    """
    try:
        token = meet_token(token_path=token_path, env=env, transport=transport)
    except AuthError as exc:
        raise ConfigError(
            f"{exc} Run `python -m meetings auth` to sign in to Google for Meet."
        ) from None
    return MeetConfig(access_token=token, **kwargs)

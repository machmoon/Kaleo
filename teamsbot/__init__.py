"""Microsoft Teams front end: a meeting states a requirement, a board comes back.

The sibling of ``zoombot/`` and the same idea as ``meetings/``: the pipeline
already turns a sentence into a PCB, and this package supplies the sentence
from a place hardware requirements are actually stated. Structured like
``service/`` -- standard library only, no engine logic here, and a Protocol
seam at every network boundary so the whole suite runs offline with no keys.

Two things are stated plainly rather than discovered later:

* **This package performs no interactive OAuth dance.** Credentials come from
  an Entra ID app registration through the environment
  (:mod:`teamsbot.config`); :mod:`teamsbot.graph` exchanges them for an app
  token with the client-credentials grant and keeps it in memory only.
* **Nothing here has been run against a live Microsoft 365 tenant**, and a
  transcript exists only when the meeting organiser enabled transcription.

Only the configuration and Graph halves are re-exported here. The rest --
``speak``, ``agent``, ``runner``, ``app`` -- is imported from its own module,
so ``import teamsbot.config`` keeps working while a sibling is still being
written or is deliberately absent from a deployment.
"""

from .config import (
    DEFAULT_GRAPH_BASE,
    REQUIRED_ENV,
    SPEAK_MODES,
    TEAMS_ENV,
    Config,
    ConfigError,
    load_config,
)
from .graph import (
    ALLOWED_HOSTS,
    GraphClient,
    Meeting,
    NoTranscriptError,
    TeamsAuthError,
    TeamsCallError,
    TeamsError,
    TeamsRefusedError,
    Transcript,
    TranscriptionDisabledError,
    TranscriptUnavailableError,
    Transport,
    UrllibTransport,
)

__all__ = [
    "Config",
    "ConfigError",
    "load_config",
    "TEAMS_ENV",
    "REQUIRED_ENV",
    "SPEAK_MODES",
    "DEFAULT_GRAPH_BASE",
    "ALLOWED_HOSTS",
    "GraphClient",
    "Meeting",
    "Transcript",
    "Transport",
    "UrllibTransport",
    "TeamsError",
    "TeamsRefusedError",
    "TeamsAuthError",
    "TeamsCallError",
    "TranscriptUnavailableError",
    "NoTranscriptError",
    "TranscriptionDisabledError",
]

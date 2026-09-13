"""Slack front end for the silkscreen pipeline.

A hardware team lives in a channel. This package puts the pipeline there: you
mention the bot with what you want built, and the run happens in a thread under
your message -- progress, the review, the board image, and the ``.kicad_pcb``
itself, all visible to everyone in the channel rather than hidden in a DM.

Layering follows the rest of the repository. Nothing here reimplements engine
behaviour: :mod:`slackbot.runner` calls :func:`silkscreen.agents.generate_pcb`
and formats what comes back. The Slack Web API client is stdlib-only, matching
``service/``, so the bot adds no dependency the engine did not already have.
"""

from .commands import Command, CommandError, parse_command
from .config import Config, ConfigError, load_config

__all__ = [
    "Command",
    "CommandError",
    "parse_command",
    "Config",
    "ConfigError",
    "load_config",
]

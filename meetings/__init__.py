"""Google Meet front end: a meeting says what it needs, a board comes back.

The pipeline already turns a sentence into a PCB. This package supplies the
sentence from a place hardware requirements are actually stated -- a standup --
instead of a text box someone remembered to fill in.

Structured like ``service/``: standard library only, no engine logic, and a
protocol seam at the network boundary so every test runs offline.
"""

from .config import ConfigError, MeetConfig
from .intent import BoardRequest, extract_requests
from .meet import Conference, MeetClient, MeetError, Transcript, TranscriptEntry
from .runner import MeetingRun, run_meeting

__all__ = [
    "MeetConfig",
    "ConfigError",
    "MeetClient",
    "MeetError",
    "Conference",
    "Transcript",
    "TranscriptEntry",
    "BoardRequest",
    "extract_requests",
    "MeetingRun",
    "run_meeting",
]

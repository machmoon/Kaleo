"""Google Meet REST API v2 client, standard library only.

**Why the REST API and not a bot that joins the call.** Every open-source
meeting bot drives a headless browser that joins as a fake participant, because
neither Meet nor Zoom offers a public join-as-a-bot API. That approach breaks
whenever the web client changes, and it puts an uninvited participant in the
room. Meet's REST API already exposes the finished transcript of a conference
that had transcription switched on, which is the only thing this integration
actually needs. No browser, no fake attendee, no media plumbing, and nothing
running while the meeting is live.

The trade is stated rather than hidden: this reads a meeting **after** it ends,
and only when the organiser enabled transcription. A live listener would need
the Meet Media API, which is a different and much larger piece of work.

The network sits behind :class:`Transport` so every test runs offline against a
recorded transport, the same seam ``Model``/``ScriptedModel`` uses in the
agents layer.

**Where the response shapes come from.** Every field name, page key, enum value
and page-size limit this module reads was checked on 2026-09-08 against
Google's own published discovery document -- ``GET
https://meet.googleapis.com/$discovery/rest?version=v2``, ``revision``
``20260903`` -- which is public and needs no credential, so a reader can
re-run the check without a Workspace tenant. Each is cited at its use below.
That covers the *shape*. What it deliberately does not cover, and what stays
unverifiable without a tenant, is Google's live *behaviour*: whether a token
with only :data:`~meetings.config.MEET_SCOPES` is really admitted (the document
says the method accepts it; only a real call proves the project and the
Workspace admin agree), and what a conference with transcription off actually
returns. Those two are called out in ``docs/`` and in the report a poll prints,
and both fail as a named :class:`MeetError` rather than as an empty transcript.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from .config import MeetConfig

__all__ = [
    "Transport",
    "UrllibTransport",
    "MeetClient",
    "MeetError",
    "Conference",
    "Transcript",
    "TranscriptEntry",
]

#: A page bigger than this is refused rather than streamed into memory. A
#: transcript is text; anything this large is a bug or a different resource.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

#: Hard cap on pages walked for one collection. An unbounded
#: ``while nextPageToken`` is one server-side bug away from an infinite loop
#: that quietly bills for every request it makes.
MAX_PAGES = 50

#: ``pageSize`` sent on every list call.
#:
#: **CONFIRMED** against Google's own published discovery document for Meet v2
#: (``https://meet.googleapis.com/$discovery/rest?version=v2``, revision
#: ``20260903``): ``conferenceRecords.list``, ``conferenceRecords.transcripts
#: .list`` and ``conferenceRecords.transcripts.entries.list`` each declare a
#: ``pageSize`` query parameter reading "The maximum value is 100; values above
#: 100 are coerced to 100."
#:
#: It is sent on *every* list, and that is the fix for a real defect rather
#: than a tuning knob. The discovery document also says the transcripts and
#: entries defaults are **10** when ``pageSize`` is unspecified, and those two
#: calls used to send none. Ten entries a page against :data:`MAX_PAGES` gave a
#: ceiling of 500 utterances for a whole conference -- a forty-minute meeting
#: is comfortably past that -- so an ordinary meeting hit the page cap and
#: raised. Loud, which is the right failure, but the run still could not read
#: the meeting. At 100 the same cap admits 5,000 utterances and the guard goes
#: back to catching the server-side bug it was written for.
PAGE_SIZE = 100

#: Transcript states from the same discovery document
#: (``schemas.Transcript.properties.state.enum``): ``STATE_UNSPECIFIED``,
#: ``STARTED``, ``ENDED``, ``FILE_GENERATED``. ``STARTED`` is documented as
#: "An active transcript session has started" -- it is being written *now*, so
#: its entries are half a meeting, exactly what ``recent_conferences`` refuses
#: an ongoing conference for. It is named here so :meth:`MeetClient
#: .transcript_text` can refuse it out loud instead of returning the half.
TRANSCRIPT_STATE_ACTIVE = "STARTED"


class MeetError(RuntimeError):
    """A Meet API call failed. Carries the status when there was one."""

    def __init__(self, message: str, *, status: int | None = None):
        self.status = status
        super().__init__(message)


class Transport(Protocol):
    """One HTTP GET. The only network surface in this package."""

    def get(self, url: str, headers: dict[str, str]) -> tuple[int, bytes]:
        ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect.

    The stdlib's default handler follows a 3xx by copying every request header
    except ``Content-*`` onto the new request -- ``Authorization`` included --
    and re-sending it to whatever host the redirect names. A bearer token
    meant for ``meet.googleapis.com`` must never travel anywhere else, so a
    redirect is handed back as an ordinary non-200 response instead
    (``googleapps/transport.py`` closes the same hole the same way).
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


class UrllibTransport:
    """The real one. Standard library, no dependency on ``requests``."""

    def __init__(self, timeout_s: float = 30.0):
        self.timeout_s = timeout_s
        self.opener = urllib.request.build_opener(_NoRedirect())

    def get(self, url: str, headers: dict[str, str]) -> tuple[int, bytes]:
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with self.opener.open(request, timeout=self.timeout_s) as response:
                return response.status, response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            # An HTTP error still has a body, and Google's carries the reason.
            # Discarding it turns a fixable "insufficient scope" into "403".
            return exc.code, exc.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.URLError as exc:
            raise MeetError(f"could not reach the Meet API: {exc.reason}") from exc


@dataclass(frozen=True)
class TranscriptEntry:
    """One utterance. ``text`` is what was said; ``participant`` is who by."""

    name: str
    participant: str
    text: str
    language_code: str = ""
    start_time: str = ""
    end_time: str = ""


@dataclass(frozen=True)
class Transcript:
    """A conference's transcript resource."""

    name: str
    state: str = ""
    start_time: str = ""
    end_time: str = ""


@dataclass(frozen=True)
class Conference:
    """One conference record: a meeting that happened."""

    name: str
    space: str = ""
    start_time: str = ""
    end_time: str = ""
    #: Set when the record carries no end time, i.e. the meeting is still live.
    ongoing: bool = False

    def ended_at(self) -> datetime | None:
        return _parse_rfc3339(self.end_time)


def _parse_rfc3339(value: str) -> datetime | None:
    """Parse Google's timestamps, tolerating the trailing Z and nanoseconds.

    Returns None rather than raising: a timestamp we cannot read is a reason to
    treat a record as undateable, not to abandon the poll.
    """
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    # Python's parser takes at most 6 fractional digits; Google sends 9, so the
    # fraction has to be truncated. Split it off by POSITION, not by filtering
    # digits out of the whole tail: an offset like "+02:00" is full of digits,
    # and scooping those into the fraction both loses the offset and invents a
    # time. "10:00:00.5+02:00" became ".502000+00:00" -- two hours wrong, and
    # silently so, because nothing downstream can tell a bad parse from a good
    # one. Millisecond timestamps failed outright for the same reason.
    if "." in text:
        head, _, tail = text.partition(".")
        fraction = ""
        for char in tail:
            if not char.isdigit():
                break
            fraction += char
        offset = tail[len(fraction):]
        text = f"{head}.{fraction[:6] or '0'}{offset}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class MeetClient:
    """Read-only access to conference records and their transcripts."""

    def __init__(
        self, config: MeetConfig, transport: Transport | None = None
    ):
        self.config = config
        self.transport = transport or UrllibTransport()

    # -- plumbing ---------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        url = f"{self.config.api_base.rstrip('/')}/{path.lstrip('/')}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        status, body = self.transport.get(
            url,
            {
                "Authorization": f"Bearer {self.config.access_token}",
                "Accept": "application/json",
            },
        )
        if len(body) > MAX_RESPONSE_BYTES:
            raise MeetError(f"response from {path} exceeds {MAX_RESPONSE_BYTES} bytes")
        if status != 200:
            raise MeetError(_explain(status, body), status=status)
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MeetError(f"{path} did not return JSON: {exc}") from exc
        if not isinstance(decoded, dict):
            raise MeetError(
                f"{path} returned {type(decoded).__name__}, expected object"
            )
        return decoded

    def _pages(self, path: str, key: str, params: dict | None = None):
        """Yield every item across pages, with a hard page cap.

        An unbounded ``while nextPageToken`` is one server-side bug away from
        an infinite loop that quietly bills for every request it makes.
        """
        seen_pages = 0
        token = None
        while seen_pages < MAX_PAGES:
            page_params = dict(params or {})
            page_params.setdefault("pageSize", PAGE_SIZE)
            if token:
                page_params["pageToken"] = token
            payload = self._get(path, page_params)
            for item in payload.get(key) or []:
                if isinstance(item, dict):
                    yield item
            token = payload.get("nextPageToken")
            seen_pages += 1
            if not token:
                return
        raise MeetError(
            f"{path} paged past {MAX_PAGES} pages of {PAGE_SIZE}; refusing to "
            f"continue. This is a refusal, not an empty result: whatever is "
            f"past here has not been read"
        )

    # -- resources --------------------------------------------------------

    def conferences(self, *, filter_expr: str = "") -> list[Conference]:
        """Conference records the token can see, in the API's own order.

        Deliberately not "newest first": no ``orderBy`` is sent and nothing is
        sorted here, so claiming an order would be a lie that
        ``recent_conferences`` callers would act on -- taking
        ``max_runs_per_poll`` off the front of an arbitrary order picks
        arbitrary meetings.
        """
        params: dict[str, Any] = {}
        if filter_expr:
            # CONFIRMED filterable fields, Meet v2 discovery document
            # (revision 20260903), conferenceRecords.list `filter` parameter:
            # `space.meeting_code`, `space.name`, `start_time`, `end_time`,
            # e.g. `space.name = "spaces/NAME"` or `end_time IS NULL`.
            params["filter"] = filter_expr
        out = []
        # CONFIRMED, Meet v2 discovery document (revision 20260903):
        # `ListConferenceRecordsResponse` carries exactly `conferenceRecords`
        # and `nextPageToken`; `ConferenceRecord` carries `name`, `startTime`,
        # `endTime`, `expireTime`, `space`. `endTime` is documented as "Set for
        # past conferences. Unset if the conference is ongoing", which is the
        # authority for the `ongoing` flag below -- it is Google's own rule,
        # not an inference from a missing field.
        for item in self._pages("conferenceRecords", "conferenceRecords", params):
            out.append(
                Conference(
                    name=str(item.get("name", "")),
                    space=str(item.get("space", "")),
                    start_time=str(item.get("startTime", "")),
                    end_time=str(item.get("endTime", "")),
                    ongoing=not item.get("endTime"),
                )
            )
        return out

    def transcripts(self, conference: str) -> list[Transcript]:
        """Transcript resources for one conference record.

        CONFIRMED, Meet v2 discovery document (revision 20260903):
        ``ListTranscriptsResponse`` carries ``transcripts`` and
        ``nextPageToken``; ``Transcript`` carries ``name``, ``state``,
        ``startTime``, ``endTime``, ``docsDestination`` and nothing else.
        """
        return [
            Transcript(
                name=str(item.get("name", "")),
                state=str(item.get("state", "")),
                start_time=str(item.get("startTime", "")),
                end_time=str(item.get("endTime", "")),
            )
            for item in self._pages(f"{conference}/transcripts", "transcripts")
        ]

    def entries(self, transcript: str) -> list[TranscriptEntry]:
        """Every utterance in a transcript, in the order Meet returns them.

        CONFIRMED, Meet v2 discovery document (revision 20260903):
        ``ListTranscriptEntriesResponse`` names the array
        **``transcriptEntries``**, not ``entries`` -- the collection is at
        ``.../entries`` but the response key is not. ``TranscriptEntry``
        carries ``name``, ``participant``, ``text``, ``languageCode``,
        ``startTime``, ``endTime``. The speaker field is literally
        ``participant`` (not ``participantId``, not ``participantSession``) and
        its value is a resource name shaped like
        ``conferenceRecords/{r}/participants/{p}``.
        """
        return [
            TranscriptEntry(
                name=str(item.get("name", "")),
                participant=str(item.get("participant", "")),
                text=str(item.get("text", "")),
                language_code=str(item.get("languageCode", "")),
                start_time=str(item.get("startTime", "")),
                end_time=str(item.get("endTime", "")),
            )
            for item in self._pages(f"{transcript}/entries", "transcriptEntries")
        ]

    def recent_conferences(self, *, now: datetime | None = None) -> list[Conference]:
        """Finished, in-scope conferences newer than the configured window.

        Ongoing conferences are skipped: their transcript is not final, and
        acting on half a meeting produces a board from half a requirement.
        """
        now = now or datetime.now(UTC)
        cutoff = now - timedelta(hours=self.config.max_age_hours)
        keep = []
        for conference in self.conferences():
            if conference.ongoing:
                continue
            if not self.config.allows(conference.space):
                continue
            ended = conference.ended_at()
            if ended is None or ended < cutoff:
                continue
            keep.append(conference)
        return keep

    def transcript_text(self, conference: str) -> str:
        """The whole conference as ``speaker: text`` lines.

        Speaker labels are Meet's opaque participant resource names, not
        display names or email addresses. That is deliberate: this integration
        never needs to know who someone is, and resolving identities would pull
        in a directory scope it has no business holding.
        """
        lines: list[str] = []
        skipped: list[str] = []
        for transcript in self.transcripts(conference):
            # A transcript in STARTED is being written right now -- Google's
            # own words are "An active transcript session has started". Reading
            # its entries yields half a meeting, which is the exact failure
            # `recent_conferences` skips an ongoing conference to avoid. Skipped
            # rather than half-read, and said out loud rather than dropped: a
            # filter nobody can see is indistinguishable from an empty meeting.
            if transcript.state.upper() == TRANSCRIPT_STATE_ACTIVE:
                skipped.append(
                    f"{transcript.name} is still being written "
                    f"(state={transcript.state}); refusing to read half of it"
                )
                continue
            for entry in self.entries(transcript.name):
                text = entry.text.strip()
                if not text:
                    continue
                speaker = entry.participant.rsplit("/", 1)[-1] or "unknown"
                lines.append(f"{speaker}: {text}")
        if not lines and skipped:
            # Never a quiet zero. An empty string here is read by
            # `runner.run_conference` as "the organiser did not turn
            # transcription on", which would be a *false* diagnosis sending
            # somebody to the wrong setting. Skipping is a different fact and
            # gets a different answer.
            raise MeetError(
                f"{conference} has no readable transcript yet: "
                + "; ".join(skipped)
            )
        return "\n".join(lines)


def _explain(status: int, body: bytes) -> str:
    """Turn an API error into something a person can act on."""
    detail = ""
    try:
        payload = json.loads(body.decode("utf-8"))
        detail = str((payload.get("error") or {}).get("message", ""))[:400]
    except Exception:
        detail = body[:200].decode("utf-8", "replace")
    hint = {
        401: " -- the access token is missing, expired, or malformed",
        403: (
            " -- the token lacks the Meet scope, or the Meet API is not "
            "enabled on this Cloud project"
        ),
        404: " -- no such conference record, or the token cannot see it",
        429: " -- rate limited by Google; back off and retry",
    }.get(status, "")
    return f"Meet API returned {status}{hint}: {detail}".rstrip(": ")

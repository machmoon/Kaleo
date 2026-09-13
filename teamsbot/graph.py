"""The Microsoft Graph boundary: tokens, meetings, transcripts. Stdlib only.

**What this is.** One narrow, read-only client for the documented Graph REST
surface a meeting agent actually needs: a client-credentials token from
``login.microsoftonline.com``, the ``onlineMeetings`` collection under a user,
the ``callTranscripts`` collection under a meeting, and a transcript's WebVTT
content. Nothing here joins a call, admits a participant or writes anything.

**What this is not.** There is no interactive OAuth dance in this package -- no
browser consent, no authorization code, no refresh token, nothing written to
disk. The Entra ID app registration's own client-credentials grant is the only
way a token is obtained, and the token lives in memory on this object for as
long as it is valid. Acquiring, rotating and storing the *client secret* is the
host's job, as in ``meetings/config.py``: a package that mints and keeps its
own credentials is a package that has to be trusted with them.

**The honest boundary, which belongs in any description of this feature.** A
transcript exists only when the meeting organiser enabled transcription, and
only after Teams has finished processing it. That is a property of the tenant
and the meeting, not something this code can arrange, which is why "no
transcript exists", "transcription was never enabled", "not authorised" and
"the call failed" are four different exception types rather than one empty
string. **None of this has been run against a live Microsoft 365 tenant from
this repo**: the network sits behind :class:`Transport`, production uses
:class:`UrllibTransport`, and every test runs offline against a recorded
transport -- the same seam ``meetings/meet.py`` and ``googleapps/transport.py``
use.

Basis, cited rather than invented: ``microsoft/BotBuilder-Samples`` for the
calling-bot shape (an Entra app registration plus a public callback endpoint)
and the documented Graph REST surface for online meetings and call transcripts
(``/users/{id}/onlineMeetings``, ``/transcripts``, ``/transcripts/{id}/content``
with ``$format=text/vtt``). Where the upstream shape and a cleverer idea
disagreed, the upstream shape won.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from .config import Config

__all__ = [
    "ALLOWED_HOSTS",
    "MAX_PAGES",
    "MAX_RESPONSE_BYTES",
    "TOKEN_SKEW_S",
    "GRAPH_SCOPE",
    "Transport",
    "UrllibTransport",
    "GraphClient",
    "Meeting",
    "Transcript",
    "TeamsError",
    "TeamsRefusedError",
    "TeamsAuthError",
    "TeamsCallError",
    "TranscriptUnavailableError",
    "NoTranscriptError",
    "TranscriptionDisabledError",
    "ensure_graph_url",
    "vtt_to_lines",
    "CredentialVerdict",
    "MEANINGS",
    "verify_credentials",
]

#: The only hosts this package will ever address. Exact matches -- a suffix
#: check would wave through ``graph.microsoft.com.evil.example``. The Azure
#: Communication Services host is deliberately absent: nothing here calls it,
#: and an allowlist entry for a host we do not use is an open door with no
#: reason to be open. The calling bot in ``teamsbot/bot/`` may add its own.
ALLOWED_HOSTS = frozenset(
    {
        "graph.microsoft.com",
        "login.microsoftonline.com",
    }
)

#: The client-credentials scope for Graph. ``/.default`` means "every
#: application permission already consented for this app registration", which
#: is the only scope form a client-credentials grant accepts.
GRAPH_SCOPE = "https://graph.microsoft.com/.default"

#: A response bigger than this is refused rather than streamed into memory. A
#: transcript is text; anything this large is a bug or a different resource.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

#: Hard cap on ``@odata.nextLink`` follows. An unbounded paging loop is one
#: server-side bug away from running forever, and every lap is a real request
#: against a real tenant's throttling budget.
MAX_PAGES = 50

#: Renew a token this many seconds before it actually expires, so a request
#: cannot start valid and arrive expired.
TOKEN_SKEW_S = 60.0


# -- errors ---------------------------------------------------------------
#
# Four questions a caller genuinely has to tell apart, so four types. An empty
# string answers all of them at once and answers none of them correctly: an
# agent handed "" concludes the meeting was silent.


class TeamsError(RuntimeError):
    """Base for every failure in this package. Carries a status when there was
    one; ``None`` means the request never left this process."""

    def __init__(self, message: str, *, status: int | None = None):
        self.status = status
        super().__init__(message)


class TeamsRefusedError(TeamsError):
    """This package refused to make the request: a non-https URL, a host
    outside :data:`ALLOWED_HOSTS`, or a redirect that would have carried the
    bearer token somewhere else."""


class TeamsAuthError(TeamsError):
    """Not authorised. The token could not be obtained, or Graph answered 401
    or 403 -- a wrong secret, a missing application permission, or (the usual
    one for transcripts) no application access policy granting this app access
    to that organiser's meetings."""


class TeamsCallError(TeamsError):
    """The call failed for some other reason: an unexpected status, a body
    that was not the JSON it claimed to be, a response too large, or paging
    past the cap."""


class TranscriptUnavailableError(TeamsError):
    """There is no transcript text to read. The subclass says why."""


class NoTranscriptError(TranscriptUnavailableError):
    """The meeting exists and this app may see it, but it has no transcript
    content -- never recorded, still processing, or an empty file. Distinct
    from :class:`TranscriptionDisabledError`, which is a decision someone made,
    and from :class:`TeamsAuthError`, which is a permission problem wearing
    the same empty-list disguise."""


class TranscriptionDisabledError(TranscriptUnavailableError):
    """Transcription was never enabled for this meeting, so no transcript can
    ever appear. Retrying is pointless; the fix is a meeting setting, and the
    caller deserves to be told that rather than to poll forever."""


# -- transport ------------------------------------------------------------


class Transport(Protocol):
    """The only network surface in this package: one GET and one POST.

    Both return ``(status, body)`` rather than raising on an HTTP error status,
    because Graph puts the useful part of a failure -- ``error.code``,
    ``error.message`` -- in the body of the 4xx.
    """

    def get(self, url: str, headers: Mapping[str, str]) -> tuple[int, bytes]: ...

    def post(
        self, url: str, headers: Mapping[str, str], body: bytes
    ) -> tuple[int, bytes]: ...


def ensure_graph_url(url: str) -> str:
    """Return ``url`` unchanged if it is https to an allowlisted host.

    Called at request-construction time rather than inside the real transport,
    so it holds for *every* transport including the fakes: code that builds a
    request for an unknown host is wrong whether or not that request would
    have left the machine.

    Raises:
        TeamsRefusedError: for any other scheme or host.
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        raise TeamsRefusedError(
            f"refusing non-https URL scheme {parsed.scheme!r} -- a bearer "
            f"token must never travel over plaintext"
        )
    if parsed.hostname not in ALLOWED_HOSTS:
        raise TeamsRefusedError(
            f"refusing to send to {parsed.hostname!r}: not a known Microsoft "
            f"host ({', '.join(sorted(ALLOWED_HOSTS))})"
        )
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect.

    The stdlib's default handler follows a 3xx by copying every request header
    except ``Content-*`` onto the new request -- ``Authorization`` included --
    and re-sending it to whatever host the redirect names. A bearer token for
    ``graph.microsoft.com`` must never travel anywhere else, so the 3xx is
    handed back as an ordinary response and the client refuses it there.

    (Graph's transcript ``content`` route can legitimately answer with a
    redirect to blob storage. That is exactly the case this rule exists for:
    following it would send the token to a host outside the allowlist. Such a
    response surfaces as :class:`TeamsRefusedError` naming the redirect, not as
    a silent empty transcript.)
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


class UrllibTransport:
    """The real one. Standard library, no dependency on ``requests`` or on
    ``msal``: two endpoints do not justify a client library, and one seam is
    easier to prove than a library's redirect policy."""

    def __init__(self, timeout_s: float = 30.0):
        self.timeout_s = timeout_s
        self.opener = urllib.request.build_opener(_NoRedirect())

    def _send(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None = None,
    ) -> tuple[int, bytes]:
        ensure_graph_url(url)
        request = urllib.request.Request(
            url, data=body, headers=dict(headers), method=method
        )
        try:
            with self.opener.open(request, timeout=self.timeout_s) as response:
                return response.status, response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            # An HTTP error still has a body, and Graph's carries the reason.
            # Discarding it turns a fixable "missing application access policy"
            # into a bare "403".
            return exc.code, exc.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.URLError as exc:
            raise TeamsCallError(
                f"could not reach {urllib.parse.urlsplit(url).hostname}: {exc.reason}"
            ) from exc

    def get(self, url: str, headers: Mapping[str, str]) -> tuple[int, bytes]:
        return self._send("GET", url, headers)

    def post(
        self, url: str, headers: Mapping[str, str], body: bytes
    ) -> tuple[int, bytes]:
        return self._send("POST", url, headers, body)


# -- values ---------------------------------------------------------------


@dataclass(frozen=True)
class Meeting:
    """One ``onlineMeeting`` resource: a meeting that exists.

    ``allow_transcription`` is tri-state on purpose. ``True`` and ``False`` are
    what the tenant said; ``None`` means the field was not in the response, and
    the difference matters -- "transcription is off" is a fact, "we do not
    know" is not, and reporting the second as the first is how a caller stops
    polling for a transcript that was going to arrive.
    """

    id: str
    subject: str = ""
    join_url: str = ""
    organizer: str = ""
    start_time: str = ""
    end_time: str = ""
    allow_transcription: bool | None = None

    def ended_at(self) -> datetime | None:
        return _parse_rfc3339(self.end_time)

    def ongoing_at(self, now: datetime) -> bool:
        """Is this meeting still running (or scheduled to be) at ``now``?

        Half a meeting is half a requirement, so an ongoing one is skipped by
        :meth:`GraphClient.recent_meetings`. A meeting with no end time is
        treated as ongoing: an unknown end is not an ended one.
        """
        ended = self.ended_at()
        return ended is None or ended > now


@dataclass(frozen=True)
class Transcript:
    """One ``callTranscript`` resource. ``content_url`` is Graph's own
    ``transcriptContentUrl``; it is recorded but never followed blindly --
    content is fetched through :meth:`GraphClient.transcript_content`, which
    goes through the allowlist like everything else."""

    id: str
    meeting_id: str = ""
    created: str = ""
    content_url: str = ""


@dataclass(frozen=True)
class _Token:
    value: str
    expires_at: datetime


# -- client ---------------------------------------------------------------


class GraphClient:
    """Read-only access to online meetings and their transcripts.

    ``clock`` is the time seam (tests drive token expiry with it) and
    ``transport`` is the network seam. Neither has a default that touches the
    outside world in a test: ``transport=None`` builds a
    :class:`UrllibTransport`, which every test replaces.
    """

    def __init__(
        self,
        config: Config,
        transport: Transport | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ):
        self.config = config
        self.transport = transport or UrllibTransport()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._token: _Token | None = None

    # -- authentication ---------------------------------------------------

    def access_token(self) -> str:
        """A valid bearer token, acquiring or renewing one as needed.

        The token is cached on this object and **never written to disk** by
        this module. It is renewed :data:`TOKEN_SKEW_S` seconds before it
        expires, so a request cannot start valid and arrive expired.
        """
        now = self._clock()
        token = self._token
        if token is not None and token.expires_at > now:
            return token.value
        self._token = self._acquire_token(now)
        return self._token.value

    def _acquire_token(self, now: datetime) -> _Token:
        """The OAuth 2.0 client-credentials grant, and nothing else.

        No user is involved, no browser opens, no code is exchanged and no
        refresh token exists: the app registration authenticates as itself
        against ``login.microsoftonline.com/{tenant}/oauth2/v2.0/token``.
        """
        url = ensure_graph_url(self.config.token_url())
        body = urllib.parse.urlencode(
            {
                "client_id": self.config.app_id,
                "client_secret": self.config.app_secret,
                "scope": GRAPH_SCOPE,
                "grant_type": "client_credentials",
            }
        ).encode("utf-8")
        try:
            status, raw = self.transport.post(
                url,
                {
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                body,
            )
        except TeamsError:
            raise
        except Exception as exc:  # a transport of someone else's making
            raise TeamsCallError(f"token request failed: {exc}") from exc

        payload = _decode(status, raw, "the token endpoint")
        if status != 200:
            detail = str(
                payload.get("error_description") or payload.get("error") or ""
            )[:400]
            raise TeamsAuthError(
                f"could not get an app token ({status}): {detail} -- check "
                f"TEAMS_APP_ID, TEAMS_APP_SECRET and TEAMS_TENANT_ID, and that "
                f"the app registration has the Graph application permissions "
                f"consented",
                status=status,
            )
        value = str(payload.get("access_token") or "")
        if not value:
            raise TeamsAuthError(
                "the token endpoint returned 200 with no access_token; "
                "refusing to continue with an empty credential"
            )
        try:
            lifetime = float(payload.get("expires_in") or 0)
        except (TypeError, ValueError):
            lifetime = 0.0
        # A missing or unreadable lifetime is treated as "expires now-ish"
        # rather than "lasts forever": re-acquiring costs one request, using a
        # dead token costs a 401 in the middle of a meeting.
        lifetime = max(lifetime - TOKEN_SKEW_S, 0.0)
        return _Token(value, now + timedelta(seconds=lifetime))

    # -- plumbing ---------------------------------------------------------

    def _url(self, path: str) -> str:
        """Absolute URL for a Graph path, checked against the allowlist.

        An already-absolute URL (an ``@odata.nextLink``, a
        ``transcriptContentUrl``) is passed through the same check -- a link in
        a response is data from the network, and data from the network does not
        get to choose which host receives the bearer token.
        """
        if path.startswith("http://") or path.startswith("https://"):
            return ensure_graph_url(path)
        base = self.config.graph_base.rstrip("/")
        return ensure_graph_url(f"{base}/{path.lstrip('/')}")

    def _fetch(self, path: str, accept: str) -> bytes:
        url = self._url(path)
        try:
            status, raw = self.transport.get(
                url,
                {
                    "Authorization": f"Bearer {self.access_token()}",
                    "Accept": accept,
                },
            )
        except TeamsError:
            raise
        except Exception as exc:
            raise TeamsCallError(f"GET {_safe(url)} failed: {exc}") from exc

        if len(raw) > MAX_RESPONSE_BYTES:
            raise TeamsCallError(
                f"{_safe(url)} returned more than {MAX_RESPONSE_BYTES} bytes; "
                f"refusing to read it into memory"
            )
        if 300 <= status < 400:
            raise TeamsRefusedError(
                f"{_safe(url)} answered {status} with a redirect, which was "
                f"not followed: a 3xx would carry the bearer token to a host "
                f"outside the allowlist",
                status=status,
            )
        if status in (401, 403):
            raise TeamsAuthError(_explain(status, raw, url), status=status)
        if status < 200 or status >= 300:
            raise TeamsCallError(_explain(status, raw, url), status=status)
        return raw

    def _get(self, path: str) -> dict[str, Any]:
        raw = self._fetch(path, "application/json")
        return _decode(200, raw, _safe(self._url(path)))

    def _pages(self, path: str) -> Iterator[dict[str, Any]]:
        """Yield every item of a Graph collection, with a hard page cap.

        An unbounded ``while @odata.nextLink`` is one server-side bug away from
        a loop that quietly bills for every request it makes and burns the
        tenant's throttling budget doing it.
        """
        seen = 0
        next_path: str | None = path
        while next_path is not None:
            if seen >= MAX_PAGES:
                raise TeamsCallError(
                    f"{_safe(self._url(path))} paged past {MAX_PAGES} pages; "
                    f"refusing to continue"
                )
            payload = self._get(next_path)
            for item in payload.get("value") or []:
                if isinstance(item, dict):
                    yield item
            link = payload.get("@odata.nextLink")
            next_path = str(link) if link else None
            seen += 1

    # -- resources --------------------------------------------------------

    def meetings(self, user_id: str, *, join_web_url: str = "") -> list[Meeting]:
        """Online meetings under one user, in the order Graph returns them.

        Deliberately not "newest first": no ``$orderby`` is sent and nothing is
        sorted here, so claiming an order would be a lie that callers would act
        on -- taking a cap off the front of an arbitrary order picks arbitrary
        meetings. ``join_web_url`` maps to Graph's documented
        ``$filter=JoinWebUrl eq '...'`` lookup.
        """
        path = f"users/{urllib.parse.quote(user_id, safe='')}/onlineMeetings"
        if join_web_url:
            escaped = join_web_url.replace("'", "''")
            path = f"{path}?$filter=JoinWebUrl%20eq%20'{urllib.parse.quote(escaped)}'"
        return [_meeting(item) for item in self._pages(path)]

    def recent_meetings(
        self,
        user_id: str,
        *,
        now: datetime | None = None,
        max_age_hours: float = 24.0,
        join_web_url: str = "",
    ) -> list[Meeting]:
        """Finished, in-scope meetings newer than ``max_age_hours``.

        The same three gates ``meetings/meet.py:recent_conferences`` applies,
        for the same reasons: an ongoing meeting is skipped because its
        transcript is not final, a meeting outside
        :meth:`Config.allows` is skipped because runs cost money, and an old
        one is skipped because a first poll against a busy tenant would
        otherwise replay months of meetings as paid pipeline runs.
        """
        now = now or self._clock()
        cutoff = now - timedelta(hours=max_age_hours)
        keep: list[Meeting] = []
        for meeting in self.meetings(user_id, join_web_url=join_web_url):
            if meeting.ongoing_at(now):
                continue
            if not self.config.allows(meeting.id):
                continue
            ended = meeting.ended_at()
            if ended is None or ended < cutoff:
                continue
            keep.append(meeting)
        return keep

    def transcripts(self, user_id: str, meeting_id: str) -> list[Transcript]:
        """Transcript resources for one meeting.

        An empty list is returned as an empty list here -- it is a truthful
        answer to "which transcripts exist" -- and turned into a specific
        exception by :meth:`transcript_text`, which was asked a question an
        empty list cannot answer.
        """
        path = (
            f"users/{urllib.parse.quote(user_id, safe='')}"
            f"/onlineMeetings/{urllib.parse.quote(meeting_id, safe='')}/transcripts"
        )
        return [
            Transcript(
                id=str(item.get("id", "")),
                meeting_id=str(item.get("meetingId", "") or meeting_id),
                created=str(item.get("createdDateTime", "")),
                content_url=str(item.get("transcriptContentUrl", "")),
            )
            for item in self._pages(path)
        ]

    def transcript_content(
        self, user_id: str, meeting_id: str, transcript_id: str
    ) -> str:
        """One transcript's WebVTT content, as text."""
        path = (
            f"users/{urllib.parse.quote(user_id, safe='')}"
            f"/onlineMeetings/{urllib.parse.quote(meeting_id, safe='')}"
            f"/transcripts/{urllib.parse.quote(transcript_id, safe='')}"
            f"/content?$format=text/vtt"
        )
        raw = self._fetch(path, "text/vtt")
        return raw.decode("utf-8", "replace")

    def transcript_text(self, user_id: str, meeting: Meeting | str) -> str:
        """The whole meeting as ``speaker: text`` lines.

        The same value ``meetings/meet.py:transcript_text`` returns, so a
        runner can treat a Teams meeting and a Meet conference identically.

        One difference is worth stating rather than hiding: Meet's transcript
        entries carry opaque participant ids, while Teams' WebVTT carries the
        speaker's **display name** inline (``<v Ada Lovelace>``). There is no id
        form in the content, so the display name is what comes out. That is a
        real difference in what this integration sees, not a choice made here.

        Raises:
            TranscriptionDisabledError: the meeting says transcription was
                never allowed, so no transcript can ever appear.
            NoTranscriptError: transcription may have been on, but there is no
                transcript content -- none recorded, still processing, or an
                empty file. Never an empty string: an agent handed ``""``
                concludes the meeting was silent.
        """
        meeting_id = meeting.id if isinstance(meeting, Meeting) else str(meeting)
        if isinstance(meeting, Meeting) and meeting.allow_transcription is False:
            raise TranscriptionDisabledError(
                f"meeting {meeting_id} has allowAllowedTranscription disabled; "
                f"no transcript will ever exist for it, so retrying is "
                f"pointless -- the organiser has to enable transcription"
            )
        found = self.transcripts(user_id, meeting_id)
        if not found:
            raise NoTranscriptError(
                f"meeting {meeting_id} has no transcript resource: either "
                f"transcription was never started, or Teams has not finished "
                f"processing it yet"
            )
        lines: list[str] = []
        for transcript in found:
            lines.extend(
                vtt_to_lines(
                    self.transcript_content(user_id, meeting_id, transcript.id)
                )
            )
        if not lines:
            raise NoTranscriptError(
                f"meeting {meeting_id} has {len(found)} transcript resource(s) "
                f"but no utterances in them"
            )
        return "\n".join(lines)


# -- parsing --------------------------------------------------------------


# -- credential check -----------------------------------------------------

#: The fixed vocabulary a verdict may carry. Entra's ``error_description`` is
#: free text that quotes the request back (it has carried the app id and the
#: correlation id, and nothing stops it carrying the secret's prefix), so a
#: verdict names the AADSTS code and one of these sentences, never the text.
MEANINGS: dict[str, str] = {
    "AADSTS7000215": "the secret is wrong",
    "AADSTS700016": "that app id is not in this tenant",
    "AADSTS90002": "tenant unknown",
}

_AADSTS = re.compile(r"AADSTS\d+")


@dataclass(frozen=True)
class CredentialVerdict:
    """Did Entra issue an app token for this registration?

    ``ok`` with ``expires_in`` (seconds) when it did; otherwise ``status`` (the
    HTTP status, or ``None`` when the request never left this process),
    ``code`` (``AADSTS<n>`` or ``""``) and ``meaning`` from :data:`MEANINGS`.
    Never the token, never the secret, never Entra's free text.
    """

    ok: bool
    status: int | None
    code: str
    meaning: str
    expires_in: int | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "code": self.code,
            "meaning": self.meaning,
            "expires_in": self.expires_in,
        }


def _meaning_for(code: str, status: int | None) -> str:
    if code in MEANINGS:
        return MEANINGS[code]
    if code:
        return f"Entra refused; code {code.removeprefix('AADSTS')}"
    if status is not None:
        return f"Entra refused; HTTP {status}"
    return "Entra refused"


def verify_credentials(
    config: Config,
    transport: Transport | None = None,
    *,
    clock: Callable[[], datetime] | None = None,
) -> CredentialVerdict:
    """One client-credentials request, and a verdict in fixed words.

    Runs :meth:`GraphClient.access_token` exactly once. A token proves the
    ids and the secret; it proves nothing about Graph permissions being
    consented, and the verdict does not claim otherwise. Never raises for a
    refusal -- a wrong secret is an answer, not an exception -- and never
    returns the token.
    """
    client = GraphClient(config, transport, clock=clock)
    now = client._clock()
    try:
        client.access_token()
    except TeamsAuthError as exc:
        match = _AADSTS.search(str(exc))
        code = match.group(0) if match else ""
        return CredentialVerdict(
            ok=False,
            status=exc.status,
            code=code,
            meaning=_meaning_for(code, exc.status),
            expires_in=None,
        )
    except TeamsRefusedError:
        return CredentialVerdict(
            ok=False,
            status=None,
            code="",
            meaning="the token endpoint is not on this package's allowlist",
            expires_in=None,
        )
    except TeamsError as exc:
        return CredentialVerdict(
            ok=False,
            status=exc.status,
            code="",
            meaning="the token endpoint could not be reached",
            expires_in=None,
        )
    token = client._token
    lifetime: int | None = None
    if token is not None:
        # ``_acquire_token`` already subtracted the skew; add it back so the
        # number matches what Entra said.
        lifetime = int((token.expires_at - now).total_seconds() + TOKEN_SKEW_S)
    return CredentialVerdict(
        ok=True, status=200, code="", meaning="token issued", expires_in=lifetime
    )


def vtt_to_lines(vtt: str) -> list[str]:
    """WebVTT to ``speaker: text`` lines, one per cue with actual words.

    Teams writes each cue's payload as ``<v Speaker Name>what they said</v>``.
    Cue headers, numbering, ``WEBVTT``/``NOTE`` blocks and blank lines carry no
    speech and are dropped; a cue with no voice tag keeps its text under an
    ``unknown`` speaker rather than vanishing, because a missing label is not a
    reason to lose a requirement someone stated out loud.
    """
    lines: list[str] = []
    for raw in vtt.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        if "-->" in line:
            continue
        if line.isdigit() or _is_cue_id(line):
            continue
        speaker, text = _voice(line)
        if text:
            lines.append(f"{speaker}: {text}")
    return lines


def _is_cue_id(line: str) -> bool:
    """A bare cue identifier: Teams uses a GUID with a suffix, no spaces and
    no sentence punctuation. Conservative on purpose -- misclassifying speech
    as a cue id loses speech, which is worse than keeping one stray id."""
    return (
        "<" not in line
        and " " not in line
        and line.count("-") >= 4
        and any(char.isdigit() for char in line)
    )


def _voice(line: str) -> tuple[str, str]:
    """Split ``<v Name>text</v>`` into its two halves."""
    if line.startswith("<v ") and ">" in line:
        head, _, rest = line.partition(">")
        speaker = head[3:].strip() or "unknown"
        text = rest.replace("</v>", "").strip()
        return speaker or "unknown", text
    return "unknown", line.replace("</v>", "").strip()


def _meeting(item: Mapping[str, Any]) -> Meeting:
    organizer = ""
    identity = item.get("participants")
    if isinstance(identity, Mapping):
        org = identity.get("organizer")
        if isinstance(org, Mapping):
            user = org.get("identity")
            if isinstance(user, Mapping):
                inner = user.get("user")
                if isinstance(inner, Mapping):
                    organizer = str(inner.get("id", ""))
    allow = item.get("allowTranscription")
    return Meeting(
        id=str(item.get("id", "")),
        subject=str(item.get("subject", "")),
        join_url=str(item.get("joinWebUrl", "")),
        organizer=organizer,
        start_time=str(item.get("startDateTime", "")),
        end_time=str(item.get("endDateTime", "")),
        allow_transcription=allow if isinstance(allow, bool) else None,
    )


def _decode(status: int, raw: bytes, what: str) -> dict[str, Any]:
    """A JSON object out of a response body, or a specific error.

    A body that is not the JSON it claimed to be is a failed call, not an empty
    dict: the proxy login page that 200s in place of an API is exactly the
    thing an empty dict would hide.
    """
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TeamsCallError(
            f"{what} returned HTTP {status} that was not JSON: "
            f"{raw[:120].decode('utf-8', 'replace')!r}",
            status=status,
        ) from exc
    if not isinstance(payload, dict):
        raise TeamsCallError(
            f"{what} returned {type(payload).__name__}, expected a JSON object",
            status=status,
        )
    return payload


def _safe(url: str) -> str:
    """A URL with its query string dropped -- a Graph query can carry a
    ``$filter`` naming a person, and this string ends up in exception text."""
    parsed = urllib.parse.urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _explain(status: int, body: bytes, url: str) -> str:
    """Turn a Graph error into something a person can act on."""
    detail = ""
    try:
        payload = json.loads(body.decode("utf-8"))
        error = payload.get("error")
        if isinstance(error, dict):
            detail = f"{error.get('code', '')}: {error.get('message', '')}"[:400]
    except Exception:
        detail = body[:200].decode("utf-8", "replace")
    hint = {
        401: (
            " -- the app token is missing, expired or malformed; check "
            "TEAMS_APP_SECRET"
        ),
        403: (
            " -- the app registration lacks the Graph application permission "
            "(OnlineMeetings.Read.All / OnlineMeetingTranscript.Read.All), or "
            "no application access policy grants it access to this "
            "organiser's meetings"
        ),
        404: " -- no such meeting or transcript, or this app cannot see it",
        429: " -- throttled by Graph; back off and retry",
    }.get(status, "")
    return f"{_safe(url)} returned {status}{hint}: {detail}".rstrip(": ")


def _parse_rfc3339(value: str) -> datetime | None:
    """Parse Graph's timestamps, tolerating the trailing Z and sub-microsecond
    precision. Returns None rather than raising: a timestamp we cannot read is
    a reason to treat a record as undateable, not to abandon the poll."""
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    if "." in text:
        head, _, tail = text.partition(".")
        fraction = ""
        for char in tail:
            if not char.isdigit():
                break
            fraction += char
        offset = tail[len(fraction) :]
        text = f"{head}.{fraction[:6] or '0'}{offset}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

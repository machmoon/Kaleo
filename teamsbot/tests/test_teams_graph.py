"""The Graph boundary, offline: tokens, allowlist, redirects, paging, errors.

Every test drives a recorded transport. Nothing here touches the network and
nothing needs a tenant -- which is also the honest statement about this
package: it has never run against a live Microsoft 365 tenant, so what is
pinned below is the request construction and the failure taxonomy, not a
successful round trip.
"""

from __future__ import annotations

import json
import urllib.parse
from datetime import UTC, datetime, timedelta

import pytest

from teamsbot.config import Config
from teamsbot.graph import (
    ALLOWED_HOSTS,
    GRAPH_SCOPE,
    MAX_PAGES,
    MAX_RESPONSE_BYTES,
    GraphClient,
    Meeting,
    NoTranscriptError,
    TeamsAuthError,
    TeamsCallError,
    TeamsError,
    TeamsRefusedError,
    TranscriptionDisabledError,
    TranscriptUnavailableError,
    ensure_graph_url,
    vtt_to_lines,
)

TENANT = "99999999-8888-7777-6666-555555555555"
TOKEN_URL = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"
USER = "organiser@example.com"
NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


def make_config(**overrides) -> Config:
    fields = {
        "app_id": "app-id",
        "app_secret": "app-secret",
        "tenant_id": TENANT,
    }
    fields.update(overrides)
    return Config(**fields)


class Recorded:
    """A transport that answers from a routing table and records every call.

    ``routes`` maps a URL (query string stripped) to either a
    ``(status, body)`` pair or a callable taking the call index. Anything not
    in the table is a test bug, and it fails loudly rather than answering 404
    -- an unexpected request that quietly succeeds is how a wrong URL survives
    a green suite.
    """

    def __init__(self, routes: dict, token: str = "tok-1"):
        self.routes = routes
        self.token = token
        self.calls: list[tuple[str, str, dict, bytes]] = []
        self.token_calls = 0

    def _answer(self, method: str, url: str, headers, body: bytes):
        self.calls.append((method, url, dict(headers), body))
        key = url.split("?", 1)[0]
        if key == TOKEN_URL:
            self.token_calls += 1
            route = self.routes.get(TOKEN_URL)
            if route is None:
                return _json(
                    200,
                    {
                        "access_token": f"{self.token}-{self.token_calls}",
                        "expires_in": 3600,
                        "token_type": "Bearer",
                    },
                )
        else:
            route = self.routes.get(key)
        if route is None:
            raise AssertionError(f"no recorded response for {method} {url}")
        if callable(route):
            return route(url, self)
        return route

    def get(self, url, headers):
        return self._answer("GET", url, headers, b"")

    def post(self, url, headers, body):
        return self._answer("POST", url, headers, body)


def _json(status: int, payload) -> tuple[int, bytes]:
    return status, json.dumps(payload).encode("utf-8")


def meetings_url(user: str = USER) -> str:
    return (
        "https://graph.microsoft.com/v1.0/users/"
        f"{urllib.parse.quote(user, safe='')}/onlineMeetings"
    )


def transcripts_url(meeting_id: str, user: str = USER) -> str:
    return f"{meetings_url(user)}/{meeting_id}/transcripts"


def content_url(meeting_id: str, transcript_id: str, user: str = USER) -> str:
    return f"{transcripts_url(meeting_id, user)}/{transcript_id}/content"


VTT = """WEBVTT

3c4d5e6f-1111-2222-3333-444455556666/1
00:00:01.000 --> 00:00:04.000
<v Ada Lovelace>we need a 3.3 volt rail for the sensor</v>

3c4d5e6f-1111-2222-3333-444455556666/2
00:00:05.000 --> 00:00:08.000
<v Charles Babbage>and a blinker to prove it boots</v>
"""


# -- the allowlist --------------------------------------------------------


def test_allowlist_is_exactly_the_two_hosts_we_call():
    assert set(ALLOWED_HOSTS) == {
        "graph.microsoft.com",
        "login.microsoftonline.com",
    }


@pytest.mark.parametrize(
    "url",
    [
        "http://graph.microsoft.com/v1.0/me",
        "https://graph.microsoft.com.evil.example/v1.0/me",
        "https://evil.example/graph.microsoft.com/v1.0/me",
        "https://login.microsoftonline.com.evil.example/x",
        "https://communication.azure.com/x",
    ],
)
def test_off_allowlist_urls_are_refused(url: str):
    with pytest.raises(TeamsRefusedError):
        ensure_graph_url(url)


def test_off_allowlist_next_link_never_reaches_the_transport():
    """A link in a response is data from the network, and data from the network
    does not get to choose which host receives the bearer token."""
    routes = {
        meetings_url(): _json(
            200,
            {
                "value": [],
                "@odata.nextLink": "https://evil.example/v1.0/steal",
            },
        )
    }
    transport = Recorded(routes)
    client = GraphClient(make_config(), transport, clock=lambda: NOW)
    with pytest.raises(TeamsRefusedError) as excinfo:
        client.meetings(USER)
    assert "evil.example" in str(excinfo.value)
    assert not any("evil.example" in url for _, url, _, _ in transport.calls)


def test_a_bad_graph_base_is_refused_before_any_request():
    # The Config version pin permits this URL, so the second line of defence
    # is the one under test: the host check at request construction.
    config = make_config(graph_base="https://graph.microsoft.com.evil.example/v1.0")
    transport = Recorded({})
    client = GraphClient(config, transport, clock=lambda: NOW)
    with pytest.raises(TeamsRefusedError):
        client.meetings(USER)
    assert transport.calls == []


# -- token acquisition, caching, refresh ----------------------------------


def test_token_is_a_client_credentials_grant_against_the_tenant_endpoint():
    transport = Recorded({})
    client = GraphClient(make_config(), transport, clock=lambda: NOW)
    assert client.access_token() == "tok-1-1"

    method, url, headers, body = transport.calls[0]
    assert method == "POST"
    assert url == TOKEN_URL
    assert headers["Content-Type"] == "application/x-www-form-urlencoded"
    form = dict(urllib.parse.parse_qsl(body.decode("utf-8")))
    assert form == {
        "client_id": "app-id",
        "client_secret": "app-secret",
        "scope": GRAPH_SCOPE,
        "grant_type": "client_credentials",
    }
    # No user, no code, no redirect URI: there is no interactive dance here.
    assert "code" not in form
    assert "redirect_uri" not in form


def test_token_is_cached_across_calls():
    transport = Recorded({})
    client = GraphClient(make_config(), transport, clock=lambda: NOW)
    first = client.access_token()
    second = client.access_token()
    assert first == second
    assert transport.token_calls == 1


def test_token_is_renewed_before_it_expires():
    now = NOW
    transport = Recorded({})
    client = GraphClient(make_config(), transport, clock=lambda: now)
    assert client.access_token() == "tok-1-1"

    # Still inside the lifetime (3600s less the 60s skew).
    now = NOW + timedelta(seconds=3000)
    assert client.access_token() == "tok-1-1"
    assert transport.token_calls == 1

    # Past the skew-adjusted expiry: a fresh grant, not a stale token.
    now = NOW + timedelta(seconds=3550)
    assert client.access_token() == "tok-1-2"
    assert transport.token_calls == 2


def test_token_with_no_lifetime_is_treated_as_expiring_immediately():
    def answer(url, transport):
        return _json(200, {"access_token": f"t{transport.token_calls}"})

    routes = {TOKEN_URL: answer}
    transport = Recorded(routes)
    client = GraphClient(make_config(), transport, clock=lambda: NOW)
    client.access_token()
    client.access_token()
    assert transport.token_calls == 2


def test_token_failure_is_an_auth_error_naming_the_settings_to_check():
    routes = {
        TOKEN_URL: _json(
            401,
            {
                "error": "invalid_client",
                "error_description": "AADSTS7000215: Invalid client secret",
            },
        )
    }
    client = GraphClient(make_config(), Recorded(routes), clock=lambda: NOW)
    with pytest.raises(TeamsAuthError) as excinfo:
        client.access_token()
    assert excinfo.value.status == 401
    assert "Invalid client secret" in str(excinfo.value)
    assert "TEAMS_APP_SECRET" in str(excinfo.value)


def test_a_token_response_with_no_token_is_refused_not_used_empty():
    routes = {TOKEN_URL: _json(200, {"expires_in": 3600})}
    client = GraphClient(make_config(), Recorded(routes), clock=lambda: NOW)
    with pytest.raises(TeamsAuthError) as excinfo:
        client.access_token()
    assert "no access_token" in str(excinfo.value)


def test_requests_carry_the_bearer_token_and_the_pinned_version():
    routes = {meetings_url(): _json(200, {"value": []})}
    transport = Recorded(routes)
    client = GraphClient(make_config(), transport, clock=lambda: NOW)
    client.meetings(USER)

    method, url, headers, _ = transport.calls[-1]
    assert method == "GET"
    assert url.startswith("https://graph.microsoft.com/v1.0/")
    assert headers["Authorization"] == "Bearer tok-1-1"


# -- redirects ------------------------------------------------------------


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_redirect_is_refused_rather_than_followed(status: int):
    """A 3xx would carry the Authorization header to whatever host it names."""
    routes = {
        meetings_url(): (status, b""),
    }
    transport = Recorded(routes)
    client = GraphClient(make_config(), transport, clock=lambda: NOW)
    with pytest.raises(TeamsRefusedError) as excinfo:
        client.meetings(USER)
    assert excinfo.value.status == status
    assert "redirect" in str(excinfo.value)
    # One GET attempted, nothing followed.
    gets = [call for call in transport.calls if call[0] == "GET"]
    assert len(gets) == 1


def test_transcript_content_redirect_is_refused_not_read_as_empty():
    routes = {
        transcripts_url("MEET-1"): _json(200, {"value": [{"id": "T1"}]}),
        content_url("MEET-1", "T1"): (302, b""),
    }
    client = GraphClient(make_config(), Recorded(routes), clock=lambda: NOW)
    with pytest.raises(TeamsRefusedError):
        client.transcript_text(USER, "MEET-1")


# -- paging ---------------------------------------------------------------


def test_paging_follows_next_link_and_yields_every_item():
    page2 = f"{meetings_url()}/page2"
    routes = {
        meetings_url(): _json(
            200, {"value": [{"id": "M1"}], "@odata.nextLink": page2}
        ),
        page2: _json(200, {"value": [{"id": "M2"}]}),
    }
    client = GraphClient(make_config(), Recorded(routes), clock=lambda: NOW)
    assert [m.id for m in client.meetings(USER)] == ["M1", "M2"]


def test_paging_stops_at_the_cap_rather_than_billing_forever():
    """A server that always returns a nextLink must not become a loop that
    quietly spends the tenant's throttling budget."""

    def endless(url, transport):
        return _json(
            200, {"value": [{"id": "M"}], "@odata.nextLink": meetings_url()}
        )

    transport = Recorded({meetings_url(): endless})
    client = GraphClient(make_config(), transport, clock=lambda: NOW)
    with pytest.raises(TeamsCallError) as excinfo:
        client.meetings(USER)
    assert str(MAX_PAGES) in str(excinfo.value)
    gets = [call for call in transport.calls if call[0] == "GET"]
    assert len(gets) == MAX_PAGES


def test_an_oversized_response_is_refused_rather_than_held_in_memory():
    routes = {meetings_url(): (200, b"x" * (MAX_RESPONSE_BYTES + 1))}
    client = GraphClient(make_config(), Recorded(routes), clock=lambda: NOW)
    with pytest.raises(TeamsCallError) as excinfo:
        client.meetings(USER)
    assert "bytes" in str(excinfo.value)


# -- the four distinguishable answers -------------------------------------


def test_error_types_are_siblings_a_caller_can_tell_apart():
    for error in (
        TeamsRefusedError,
        TeamsAuthError,
        TeamsCallError,
        TranscriptUnavailableError,
    ):
        assert issubclass(error, TeamsError)
    assert not issubclass(TeamsCallError, TeamsAuthError)
    assert not issubclass(TeamsAuthError, TeamsCallError)
    assert not issubclass(TranscriptUnavailableError, TeamsCallError)
    assert issubclass(NoTranscriptError, TranscriptUnavailableError)
    assert issubclass(TranscriptionDisabledError, TranscriptUnavailableError)
    assert not issubclass(NoTranscriptError, TranscriptionDisabledError)
    assert not issubclass(TranscriptionDisabledError, NoTranscriptError)


@pytest.mark.parametrize("status", [401, 403])
def test_not_authorised_is_an_auth_error_with_an_actionable_hint(status: int):
    routes = {
        transcripts_url("MEET-1"): _json(
            status,
            {"error": {"code": "Forbidden", "message": "Application is not allowed"}},
        )
    }
    client = GraphClient(make_config(), Recorded(routes), clock=lambda: NOW)
    with pytest.raises(TeamsAuthError) as excinfo:
        client.transcripts(USER, "MEET-1")
    assert excinfo.value.status == status
    assert "Application is not allowed" in str(excinfo.value)


def test_a_failed_call_is_a_call_error_and_not_an_auth_error():
    routes = {
        transcripts_url("MEET-1"): _json(
            500, {"error": {"code": "InternalError", "message": "boom"}}
        )
    }
    client = GraphClient(make_config(), Recorded(routes), clock=lambda: NOW)
    with pytest.raises(TeamsCallError) as excinfo:
        client.transcripts(USER, "MEET-1")
    assert excinfo.value.status == 500
    assert not isinstance(excinfo.value, TeamsAuthError)


def test_a_non_json_body_is_a_failed_call_not_an_empty_result():
    routes = {meetings_url(): (200, b"<html>sign in</html>")}
    client = GraphClient(make_config(), Recorded(routes), clock=lambda: NOW)
    with pytest.raises(TeamsCallError) as excinfo:
        client.meetings(USER)
    assert "not JSON" in str(excinfo.value)


def test_no_transcript_resource_raises_rather_than_returning_empty_text():
    routes = {transcripts_url("MEET-1"): _json(200, {"value": []})}
    client = GraphClient(make_config(), Recorded(routes), clock=lambda: NOW)
    with pytest.raises(NoTranscriptError) as excinfo:
        client.transcript_text(USER, "MEET-1")
    assert "processing" in str(excinfo.value)
    # The collection itself is still allowed to be empty: that is a truthful
    # answer to "which transcripts exist".
    assert client.transcripts(USER, "MEET-1") == []


def test_transcription_disabled_is_its_own_answer_and_costs_no_request():
    meeting = Meeting(id="MEET-1", allow_transcription=False)
    transport = Recorded({})
    client = GraphClient(make_config(), transport, clock=lambda: NOW)
    with pytest.raises(TranscriptionDisabledError) as excinfo:
        client.transcript_text(USER, meeting)
    assert "retrying is" in str(excinfo.value)
    assert transport.calls == [], "a settled 'no' needs no round trip"


def test_unknown_transcription_setting_still_asks_graph():
    """``None`` means we do not know, and "we do not know" must not be
    reported as "transcription is off"."""
    meeting = Meeting(id="MEET-1", allow_transcription=None)
    routes = {transcripts_url("MEET-1"): _json(200, {"value": []})}
    client = GraphClient(make_config(), Recorded(routes), clock=lambda: NOW)
    with pytest.raises(NoTranscriptError):
        client.transcript_text(USER, meeting)


def test_a_transcript_with_no_utterances_is_not_a_silent_meeting():
    routes = {
        transcripts_url("MEET-1"): _json(200, {"value": [{"id": "T1"}]}),
        content_url("MEET-1", "T1"): (200, b"WEBVTT\n\n"),
    }
    client = GraphClient(make_config(), Recorded(routes), clock=lambda: NOW)
    with pytest.raises(NoTranscriptError) as excinfo:
        client.transcript_text(USER, "MEET-1")
    assert "no utterances" in str(excinfo.value)


# -- transcripts, the happy path ------------------------------------------


def test_transcript_text_reads_like_the_meet_client_does():
    routes = {
        transcripts_url("MEET-1"): _json(
            200,
            {
                "value": [
                    {
                        "id": "T1",
                        "meetingId": "MEET-1",
                        "createdDateTime": "2026-09-06T11:00:00.0000000Z",
                        "transcriptContentUrl": "https://graph.microsoft.com/x",
                    }
                ]
            },
        ),
        content_url("MEET-1", "T1"): (200, VTT.encode("utf-8")),
    }
    transport = Recorded(routes)
    client = GraphClient(make_config(), transport, clock=lambda: NOW)

    text = client.transcript_text(USER, "MEET-1")
    assert text == (
        "Ada Lovelace: we need a 3.3 volt rail for the sensor\n"
        "Charles Babbage: and a blinker to prove it boots"
    )
    # The documented content route, with the format Graph documents.
    content_calls = [url for _, url, _, _ in transport.calls if "/content" in url]
    assert content_calls and "$format=text/vtt" in content_calls[0]


def test_transcript_metadata_is_carried_through():
    routes = {
        transcripts_url("MEET-1"): _json(
            200,
            {
                "value": [
                    {
                        "id": "T1",
                        "createdDateTime": "2026-09-06T11:00:00Z",
                        "transcriptContentUrl": "https://graph.microsoft.com/x",
                    }
                ]
            },
        )
    }
    client = GraphClient(make_config(), Recorded(routes), clock=lambda: NOW)
    (transcript,) = client.transcripts(USER, "MEET-1")
    assert transcript.id == "T1"
    assert transcript.meeting_id == "MEET-1"
    assert transcript.created.startswith("2026-09-06")
    assert transcript.content_url == "https://graph.microsoft.com/x"


@pytest.mark.parametrize(
    "vtt, expected",
    [
        ("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<v A>hi</v>\n", ["A: hi"]),
        ("<v  >mystery</v>", ["unknown: mystery"]),
        ("just a line with no voice tag", ["unknown: just a line with no voice tag"]),
        ("NOTE something\n\nWEBVTT\n", []),
    ],
)
def test_vtt_parsing_keeps_speech_and_drops_scaffolding(vtt, expected):
    assert vtt_to_lines(vtt) == expected


# -- recent_meetings ------------------------------------------------------


def _meeting_json(mid: str, start: str, end: str) -> dict:
    return {
        "id": mid,
        "subject": mid,
        "joinWebUrl": f"https://teams.microsoft.com/l/{mid}",
        "startDateTime": start,
        "endDateTime": end,
        "allowTranscription": True,
        "participants": {
            "organizer": {"identity": {"user": {"id": "org-1"}}}
        },
    }


def test_recent_meetings_skips_ongoing_old_and_out_of_scope():
    routes = {
        meetings_url(): _json(
            200,
            {
                "value": [
                    _meeting_json(
                        "FRESH", "2026-09-06T10:00:00Z", "2026-09-06T11:00:00Z"
                    ),
                    _meeting_json(
                        "ONGOING", "2026-09-06T11:30:00Z", "2026-09-06T13:00:00Z"
                    ),
                    _meeting_json(
                        "STALE", "2026-09-01T10:00:00Z", "2026-09-01T11:00:00Z"
                    ),
                    _meeting_json(
                        "OTHER", "2026-09-06T09:00:00Z", "2026-09-06T09:30:00Z"
                    ),
                ]
            },
        )
    }
    config = make_config(meeting_allowlist=("FRESH", "ONGOING", "STALE"))
    client = GraphClient(config, Recorded(routes), clock=lambda: NOW)
    kept = client.recent_meetings(USER, now=NOW, max_age_hours=24.0)
    assert [m.id for m in kept] == ["FRESH"]


def test_a_meeting_with_no_end_time_counts_as_ongoing():
    meeting = Meeting(id="M", end_time="")
    assert meeting.ongoing_at(NOW)
    assert meeting.ended_at() is None


def test_meeting_fields_are_read_from_the_documented_shape():
    routes = {
        meetings_url(): _json(
            200,
            {
                "value": [
                    _meeting_json(
                        "MEET-1", "2026-09-06T10:00:00Z", "2026-09-06T11:00:00Z"
                    )
                ]
            },
        )
    }
    client = GraphClient(make_config(), Recorded(routes), clock=lambda: NOW)
    (meeting,) = client.meetings(USER)
    assert meeting.id == "MEET-1"
    assert meeting.join_url.endswith("MEET-1")
    assert meeting.organizer == "org-1"
    assert meeting.allow_transcription is True
    assert meeting.ended_at() == datetime(2026, 9, 6, 11, 0, tzinfo=UTC)


def test_missing_allow_transcription_is_unknown_not_false():
    routes = {meetings_url(): _json(200, {"value": [{"id": "MEET-1"}]})}
    client = GraphClient(make_config(), Recorded(routes), clock=lambda: NOW)
    (meeting,) = client.meetings(USER)
    assert meeting.allow_transcription is None


def test_join_web_url_filter_is_sent_as_graph_documents_it():
    url = "https://teams.microsoft.com/l/meetup-join/abc"
    routes = {meetings_url(): _json(200, {"value": []})}
    transport = Recorded(routes)
    client = GraphClient(make_config(), transport, clock=lambda: NOW)
    client.meetings(USER, join_web_url=url)
    sent = [u for _, u, _, _ in transport.calls if "onlineMeetings" in u][0]
    assert "$filter=JoinWebUrl" in urllib.parse.unquote(sent)


# -- verify_credentials ----------------------------------------------------
#
# The Setup Assistant's Microsoft check: one token request, a verdict in a
# fixed vocabulary. What it must never carry is the token, the secret, or
# Entra's free-text ``error_description`` (which quotes the request back).


def test_verify_credentials_ok_never_returns_the_token():
    from teamsbot.graph import CredentialVerdict, verify_credentials

    transport = Recorded({}, token="SENTINEL-TOKEN")
    verdict = verify_credentials(
        make_config(app_secret="SENTINEL-SECRET"), transport, clock=lambda: NOW
    )
    assert isinstance(verdict, CredentialVerdict)
    assert verdict.ok is True and verdict.status == 200 and verdict.code == ""
    assert verdict.expires_in == 3600
    blob = json.dumps(verdict.as_dict())
    assert "SENTINEL" not in blob
    assert transport.token_calls == 1
    assert [c[1] for c in transport.calls] == [TOKEN_URL]


def test_verify_credentials_401_is_scrubbed_to_a_code_and_a_sentence():
    from teamsbot.graph import verify_credentials

    description = (
        "AADSTS7000215: Invalid client secret provided. Ensure the secret "
        "being sent in the request is the client secret value, not the client "
        "secret ID, for a secret added to app 'app-id'. Trace ID: t-1"
    )
    transport = Recorded(
        {
            TOKEN_URL: _json(
                401, {"error": "invalid_client", "error_description": description}
            )
        }
    )
    verdict = verify_credentials(
        make_config(app_secret="SENTINEL-SECRET"), transport, clock=lambda: NOW
    )
    assert verdict.as_dict() == {
        "ok": False,
        "status": 401,
        "code": "AADSTS7000215",
        "meaning": "the secret is wrong",
        "expires_in": None,
    }
    blob = json.dumps(verdict.as_dict())
    assert "SENTINEL" not in blob and "Trace ID" not in blob and "app-id" not in blob


@pytest.mark.parametrize(
    ("code", "meaning"),
    [
        ("AADSTS700016", "that app id is not in this tenant"),
        ("AADSTS90002", "tenant unknown"),
        ("AADSTS50059", "Entra refused; code 50059"),
    ],
)
def test_verify_credentials_maps_known_codes(code, meaning):
    from teamsbot.graph import verify_credentials

    payload = {"error": "x", "error_description": f"{code}: free text"}
    transport = Recorded({TOKEN_URL: _json(400, payload)})
    verdict = verify_credentials(make_config(), transport, clock=lambda: NOW)
    assert (verdict.ok, verdict.code, verdict.meaning) == (False, code, meaning)
    assert "free text" not in verdict.meaning


def test_verify_credentials_transport_failure_is_a_verdict_not_a_raise():
    from teamsbot.graph import verify_credentials

    class Down:
        def post(self, url, headers, body):
            raise ConnectionError("no route to host")

        def get(self, url, headers):  # pragma: no cover
            raise AssertionError

    verdict = verify_credentials(make_config(), Down(), clock=lambda: NOW)
    assert verdict.ok is False and verdict.status is None and verdict.code == ""
    assert verdict.meaning == "the token endpoint could not be reached"


def test_verify_credentials_refuses_an_off_allowlist_token_url(monkeypatch):
    """The URL comes from the tenant id; a crafted one must not get a POST."""
    from teamsbot.config import Config
    from teamsbot.graph import verify_credentials

    monkeypatch.setattr(
        Config, "token_url", lambda self: "https://evil.example/oauth2/v2.0/token"
    )
    transport = Recorded({})
    verdict = verify_credentials(make_config(), transport, clock=lambda: NOW)
    assert verdict.ok is False
    assert "allowlist" in verdict.meaning
    assert transport.calls == []

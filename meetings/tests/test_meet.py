"""Meet client tests.

The bug class this file guards is **the quiet half-answer**. Every failure
mode of an HTTP client that reads meetings looks like success from the inside:
a paginated list that stops after page one returns a transcript that is merely
short, not obviously wrong; a 403 swallowed into an empty list reads as "no
meetings today"; an unparseable timestamp that raises would at least be
visible, but one that silently drops a conference is not. Nothing here asserts
"a call was made" -- every test asserts on what came back, or on the exact
bytes of the request that went out.

Two of these are security assertions rather than correctness ones, and are
written explicitly for that reason: every request must carry the bearer token,
and the token must never end up in a URL (URLs are logged, proxied and
retained; Authorization headers are not).

No test in this file opens a socket. The client's only network surface is the
``Transport`` protocol, and every test injects
:class:`~meetings.tests.fakes.FakeTransport`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from meetings.config import DEFAULT_API_BASE, ConfigError, MeetConfig
from meetings.meet import (
    MAX_RESPONSE_BYTES,
    PAGE_SIZE,
    Conference,
    MeetClient,
    MeetError,
    _parse_rfc3339,
)
from meetings.tests.fakes import FakeTransport, page, paged

TOKEN = "ya29.test-token"
BASE = "https://meet.googleapis.com/v2"

CONF = "conferenceRecords/abc"
TRANSCRIPT = f"{CONF}/transcripts/t1"


def config(**overrides) -> MeetConfig:
    """A valid config pointed at a host that does not exist."""
    fields = {"access_token": TOKEN, "api_base": BASE}
    fields.update(overrides)
    return MeetConfig(**fields)


def client(routes) -> tuple[MeetClient, FakeTransport]:
    transport = FakeTransport(routes)
    return MeetClient(config(), transport), transport


def entry(participant: str, text: str, index: int = 1) -> dict:
    return {
        "name": f"{TRANSCRIPT}/entries/{index}",
        "participant": participant,
        "text": text,
        "languageCode": "en-US",
    }


def conference(
    name: str = CONF,
    *,
    space: str = "spaces/aaa",
    end_time: str | None = "2026-08-31T10:00:00.000000000Z",
) -> dict:
    record = {
        "name": name,
        "space": space,
        "startTime": "2026-08-31T09:00:00.000000000Z",
    }
    if end_time is not None:
        record["endTime"] = end_time
    return record


# -- authorisation ---------------------------------------------------------


def test_every_request_carries_the_configured_token_as_a_bearer_header():
    """Security assertion: an unauthenticated request is not a soft failure.

    Checked across all three endpoints in one run, because the header is
    assembled in ``_get`` and a regression there would be invisible to a test
    that only exercised one resource.
    """
    api, transport = client(
        {
            "/transcripts": page("transcripts", [{"name": TRANSCRIPT}]),
            "/entries": page("transcriptEntries", [entry("p/1", "hello")]),
            "conferenceRecords?": page("conferenceRecords", [conference()]),
        }
    )
    api.conferences()
    api.transcript_text(CONF)

    assert len(transport.requests) >= 3
    for request in transport.requests:
        assert request.authorization == f"Bearer {TOKEN}"
        # Accept matters too: without it Google may negotiate a non-JSON
        # representation and the decode failure would look like a bad token.
        assert request.headers.get("Accept") == "application/json"


def test_the_bearer_token_never_appears_in_a_url():
    """Security assertion: URLs are logged and proxied; headers are not.

    A refactor that "simplified" auth into an ``access_token=`` query
    parameter would still pass every functional test in this file.
    """
    api, transport = client(
        {"conferenceRecords": page("conferenceRecords", [conference()])}
    )
    api.conferences()

    assert transport.urls
    for url in transport.urls:
        assert TOKEN not in url


def test_a_request_goes_to_the_configured_api_base_and_not_a_hardcoded_host():
    api, transport = client(
        {"conferenceRecords": page("conferenceRecords", [])}
    )
    api.conferences()

    assert transport.urls[0].startswith(f"{BASE}/conferenceRecords")


# -- pagination ------------------------------------------------------------


def test_transcript_entries_are_concatenated_across_every_page():
    """Three pages in, one ordered list out -- a short transcript is a lie."""
    api, transport = client(
        {
            "/entries": paged(
                "transcriptEntries",
                [
                    [entry("participants/a", "one", 1)],
                    [entry("participants/b", "two", 2)],
                    [entry("participants/a", "three", 3)],
                ],
            )
        }
    )
    entries = api.entries(TRANSCRIPT)

    assert [e.text for e in entries] == ["one", "two", "three"]
    assert len(transport.requests) == 3


def test_a_later_page_request_replays_the_token_the_previous_page_returned():
    """The token is the whole mechanism; sending a stale or absent one silently
    re-reads page one forever (or, worse, stops)."""
    api, transport = client(
        {
            "/entries": paged(
                "transcriptEntries",
                [[entry("participants/a", "one", 1)], [entry("p/b", "two", 2)]],
            )
        }
    )
    api.entries(TRANSCRIPT)

    first, second = transport.requests
    # The first page must NOT carry a token -- sending an empty one is an
    # error at Google's end, not a no-op.
    assert "pageToken" not in first.query
    assert second.query["pageToken"] == "tok-1"


def test_transcripts_are_concatenated_across_pages():
    api, _ = client(
        {
            "/transcripts": paged(
                "transcripts",
                [
                    [{"name": f"{CONF}/transcripts/t1", "state": "ENDED"}],
                    [{"name": f"{CONF}/transcripts/t2", "state": "ENDED"}],
                ],
            )
        }
    )
    transcripts = api.transcripts(CONF)

    assert [t.name for t in transcripts] == [
        f"{CONF}/transcripts/t1",
        f"{CONF}/transcripts/t2",
    ]
    assert all(t.state == "ENDED" for t in transcripts)


def test_conference_records_are_concatenated_across_pages():
    api, _ = client(
        {
            "conferenceRecords": paged(
                "conferenceRecords",
                [
                    [conference("conferenceRecords/one")],
                    [conference("conferenceRecords/two")],
                ],
            )
        }
    )
    names = [c.name for c in api.conferences()]

    assert names == ["conferenceRecords/one", "conferenceRecords/two"]


def test_a_server_that_never_stops_paging_raises_instead_of_looping_forever():
    """A page cursor that always points at another page is one server bug away
    from an unbounded, billable loop. The cap must surface as a MeetError, not
    as a hung process."""

    def always_another_page(url, headers):
        return page("transcriptEntries", [entry("p/a", "again")], "tok-next")

    api, transport = client({"/entries": always_another_page})

    with pytest.raises(MeetError) as excinfo:
        api.entries(TRANSCRIPT)

    assert "50 pages" in str(excinfo.value)
    # And it stopped at the cap rather than somewhere near it.
    assert len(transport.requests) == 50


# -- error mapping ---------------------------------------------------------


@pytest.mark.parametrize(
    "status,hint",
    [
        (401, "expired"),
        (403, "scope"),
        (404, "no such conference record"),
        (429, "rate limited"),
    ],
)
def test_an_api_error_is_explained_in_terms_of_what_to_do_about_it(status, hint):
    """A bare "Meet API returned 403" is not actionable; naming the missing
    scope or the disabled API is. The hint is the product here."""
    api, _ = client(
        {
            "conferenceRecords": (
                status,
                {"error": {"message": "Request had insufficient authentication."}},
            )
        }
    )

    with pytest.raises(MeetError) as excinfo:
        api.conferences()

    message = str(excinfo.value)
    assert hint in message
    assert str(status) in message
    # The server's own message survives: it names the real cause more often
    # than our hint does, and discarding it loses the only specific detail.
    assert "insufficient authentication" in message
    assert excinfo.value.status == status


def test_an_unmapped_status_still_raises_with_the_status_attached():
    """No hint exists for a 500, but a caller that retries on 5xx needs
    ``.status`` to be a number rather than None."""
    api, _ = client({"conferenceRecords": (500, b"upstream exploded")})

    with pytest.raises(MeetError) as excinfo:
        api.conferences()

    assert excinfo.value.status == 500
    # A non-JSON error body is reported as text rather than swallowed.
    assert "upstream exploded" in str(excinfo.value)


def test_a_body_that_is_not_json_raises_a_meet_error_not_a_value_error():
    """An HTML sign-in page with a 200 status is what a captive proxy returns.
    A bare JSONDecodeError escaping the package would bypass every caller's
    ``except MeetError``."""
    api, _ = client({"conferenceRecords": b"<html>sign in</html>"})

    with pytest.raises(MeetError) as excinfo:
        api.conferences()

    assert "did not return JSON" in str(excinfo.value)


def test_a_json_array_where_an_object_was_expected_raises_a_meet_error():
    """``payload.get(key)`` on a list is an AttributeError three frames down;
    the shape has to be checked at the boundary."""
    # Passed as raw bytes on purpose: a Python list in the script means "one
    # response per request", which is a different feature of FakeTransport.
    api, _ = client({"conferenceRecords": b'[{"name": "nope"}]'})

    with pytest.raises(MeetError) as excinfo:
        api.conferences()

    assert "expected object" in str(excinfo.value)


def test_an_oversized_body_is_refused_rather_than_parsed():
    """A transcript is text. Anything at this size is a different resource or
    a bug, and json.loads on it would be the first OOM nobody planned for."""
    api, _ = client({"conferenceRecords": b"x" * (MAX_RESPONSE_BYTES + 1)})

    with pytest.raises(MeetError) as excinfo:
        api.conferences()

    assert "exceeds" in str(excinfo.value)


def test_a_body_exactly_at_the_limit_is_still_parsed():
    """The refusal is ``>``, not ``>=``: a boundary-off-by-one here would
    reject legitimate long meetings at an arbitrary size."""
    padding = " " * (MAX_RESPONSE_BYTES - len('{"conferenceRecords":[]}'))
    body = ('{"conferenceRecords":[]}' + padding).encode("utf-8")
    assert len(body) == MAX_RESPONSE_BYTES

    api, _ = client({"conferenceRecords": body})

    assert api.conferences() == []


# -- timestamps ------------------------------------------------------------


def test_a_trailing_z_is_understood_as_utc():
    parsed = _parse_rfc3339("2026-08-31T10:00:00Z")

    assert parsed == datetime(2026, 8, 31, 10, 0, tzinfo=UTC)
    # Naive datetimes compare-explode against aware ones later in
    # recent_conferences; the tzinfo is the load-bearing part.
    assert parsed.tzinfo is not None


def test_googles_nine_digit_nanoseconds_are_truncated_rather_than_rejected():
    """Python's fromisoformat takes at most six fractional digits and Meet
    sends nine. Without the truncation every single Meet timestamp would be
    unparseable -- and unparseable means every conference is skipped."""
    parsed = _parse_rfc3339("2026-08-31T10:00:00.123456789Z")

    assert parsed == datetime(2026, 8, 31, 10, 0, 0, 123456, tzinfo=UTC)


def test_a_non_utc_offset_survives_the_nanosecond_truncation():
    """The offset lives on the far side of the fractional digits, so a naive
    truncation eats it and silently shifts the timestamp by hours."""
    parsed = _parse_rfc3339("2026-08-31T10:00:00.123456789+02:00")

    assert parsed.utcoffset() == timedelta(hours=2)
    assert parsed.astimezone(UTC).hour == 8


def test_a_missing_timestamp_is_none_rather_than_an_exception():
    assert _parse_rfc3339("") is None


def test_garbage_is_none_rather_than_an_exception():
    """A timestamp we cannot read means "undateable record", not "abandon the
    poll" -- one malformed record must not take the whole run down."""
    assert _parse_rfc3339("not a timestamp") is None
    assert _parse_rfc3339("2026-13-45T99:99:99Z") is None


def test_a_conference_reports_its_end_time_as_a_parsed_datetime():
    ended = Conference(
        name=CONF, end_time="2026-08-31T10:00:00.000000000Z"
    ).ended_at()

    assert ended == datetime(2026, 8, 31, 10, 0, tzinfo=UTC)


# -- recent_conferences ----------------------------------------------------

#: Every filtering test pins ``now`` explicitly. A wall-clock-dependent test
#: is a flaky test: with a real ``datetime.now`` these would pass today and
#: fail whenever the fixture timestamps aged past max_age_hours.
NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def recent(conferences, **config_overrides):
    api, transport = client(
        {"conferenceRecords": page("conferenceRecords", conferences)}
    )
    api.config = config(**config_overrides)
    return api.recent_conferences(now=NOW), transport


def test_an_ongoing_conference_is_skipped_because_its_transcript_is_not_final():
    """A record with no endTime is a meeting still in progress. Acting on half
    a meeting produces a board from half a requirement."""
    kept, _ = recent(
        [
            conference("conferenceRecords/live", end_time=None),
            conference("conferenceRecords/done"),
        ]
    )

    assert [c.name for c in kept] == ["conferenceRecords/done"]


def test_a_conference_outside_the_space_allowlist_is_skipped():
    """The allowlist is the blast radius. Without it, one over-broad token
    turns every meeting in the org into a paid pipeline run."""
    kept, _ = recent(
        [
            conference("conferenceRecords/ours", space="spaces/ours"),
            conference("conferenceRecords/theirs", space="spaces/theirs"),
        ],
        space_allowlist=frozenset({"spaces/ours"}),
    )

    assert [c.name for c in kept] == ["conferenceRecords/ours"]


def test_an_empty_allowlist_admits_every_conference_the_token_can_see():
    kept, _ = recent(
        [
            conference("conferenceRecords/a", space="spaces/a"),
            conference("conferenceRecords/b", space="spaces/b"),
        ]
    )

    assert len(kept) == 2


def test_a_conference_older_than_the_age_window_is_skipped():
    """The window is what stops a first run replaying months of meetings."""
    kept, _ = recent(
        [
            conference(
                "conferenceRecords/fresh",
                end_time="2026-08-31T11:00:00.000000000Z",
            ),
            conference(
                "conferenceRecords/stale",
                end_time="2026-08-30T11:00:00.000000000Z",
            ),
        ],
        max_age_hours=2.0,
    )

    assert [c.name for c in kept] == ["conferenceRecords/fresh"]


def test_the_age_window_is_measured_from_the_caller_supplied_now():
    """Same records, a ``now`` a week later, and nothing survives -- which is
    the proof that the cutoff comes from the argument and not the clock."""
    api, _ = client(
        {
            "conferenceRecords": page(
                "conferenceRecords",
                [conference(end_time="2026-08-31T11:00:00.000000000Z")],
            )
        }
    )

    assert api.recent_conferences(now=NOW) != []
    assert api.recent_conferences(now=NOW + timedelta(days=7)) == []


def test_a_conference_with_an_unreadable_end_time_is_skipped_not_crashed():
    """``ended_at()`` returning None must filter the record out; comparing
    None to a datetime would raise and lose every other conference in the
    batch."""
    kept, _ = recent(
        [
            conference("conferenceRecords/bad", end_time="whenever"),
            conference("conferenceRecords/good"),
        ]
    )

    assert [c.name for c in kept] == ["conferenceRecords/good"]


# -- transcript_text -------------------------------------------------------


def transcript_client(entries, transcripts=None):
    return client(
        {
            "/transcripts": page(
                "transcripts", transcripts or [{"name": TRANSCRIPT}]
            ),
            "/entries": page("transcriptEntries", entries),
        }
    )


def test_entries_are_joined_as_speaker_colon_text_lines():
    api, _ = transcript_client(
        [
            entry("conferenceRecords/abc/participants/alice", "we need a buck", 1),
            entry("conferenceRecords/abc/participants/bob", "five volts in", 2),
        ]
    )

    assert api.transcript_text(CONF) == "alice: we need a buck\nbob: five volts in"


def test_only_the_last_path_segment_of_the_participant_name_is_used():
    """The full resource name is a paragraph of Google plumbing. Prefixing
    every line with it would dominate the prompt the pipeline eventually
    sees."""
    api, _ = transcript_client(
        [entry("conferenceRecords/abc/participants/xyz-123", "hello")]
    )

    assert api.transcript_text(CONF) == "xyz-123: hello"


def test_a_blank_entry_contributes_no_line():
    """Meet emits empty and whitespace-only entries around silence. Each one
    would otherwise become a bare "speaker: " line -- noise that reads to a
    model as someone saying nothing on purpose."""
    api, _ = transcript_client(
        [
            entry("participants/alice", "first", 1),
            entry("participants/bob", "", 2),
            entry("participants/carol", "   \n ", 3),
            entry("participants/alice", "last", 4),
        ]
    )

    assert api.transcript_text(CONF) == "alice: first\nalice: last"


def test_an_entry_with_no_participant_is_attributed_to_unknown():
    """An empty participant would ``rsplit`` to "" and produce a line starting
    with a colon, which reads as a formatting bug rather than missing data."""
    api, _ = transcript_client([entry("", "who said that")])

    assert api.transcript_text(CONF) == "unknown: who said that"


def test_a_conference_with_no_transcripts_yields_an_empty_string():
    """Transcription off means no transcript, which is a legitimate outcome
    and not an error."""
    api, _ = client({"/transcripts": page("transcripts", [])})

    assert api.transcript_text(CONF) == ""


def test_every_transcript_of_a_conference_is_included_in_order():
    """A conference that was stopped and restarted has more than one
    transcript resource; reading only the first loses the second half."""
    transport = FakeTransport(
        {
            "/transcripts": page(
                "transcripts",
                [
                    {"name": f"{CONF}/transcripts/t1"},
                    {"name": f"{CONF}/transcripts/t2"},
                ],
            ),
            "/transcripts/t1/entries": page(
                "transcriptEntries", [entry("participants/a", "part one")]
            ),
            "/transcripts/t2/entries": page(
                "transcriptEntries", [entry("participants/b", "part two")]
            ),
        }
    )
    api = MeetClient(config(), transport)

    assert api.transcript_text(CONF) == "a: part one\nb: part two"


# -- configuration ---------------------------------------------------------


def test_from_env_without_a_token_names_the_variable_it_wants():
    """An error that says "not configured" sends someone reading source. One
    that says MEET_ACCESS_TOKEN sends them to their shell."""
    with pytest.raises(ConfigError) as excinfo:
        MeetConfig.from_env({})

    assert "MEET_ACCESS_TOKEN" in str(excinfo.value)


def test_a_blank_token_is_rejected_the_same_as_a_missing_one():
    """``MEET_ACCESS_TOKEN=""`` is the shape a broken deploy script leaves
    behind, and it must not start."""
    with pytest.raises(ConfigError):
        MeetConfig(access_token="   ")


def test_a_non_https_api_base_is_refused_because_a_bearer_token_is_a_password():
    """Security assertion: a token on a plaintext connection is a token
    disclosed. This has to fail at construction, not at first request."""
    with pytest.raises(ConfigError) as excinfo:
        MeetConfig(access_token=TOKEN, api_base="http://meet.googleapis.com/v2")

    assert "https" in str(excinfo.value)


def test_meet_spaces_parses_a_comma_separated_list_and_trims_whitespace():
    parsed = MeetConfig.from_env(
        {"MEET_ACCESS_TOKEN": TOKEN, "MEET_SPACES": "spaces/a, spaces/b ,,spaces/c"}
    )

    # The empty element between the two commas is dropped rather than becoming
    # an "" entry that no space could ever match.
    assert parsed.space_allowlist == frozenset(
        {"spaces/a", "spaces/b", "spaces/c"}
    )


def test_an_empty_allowlist_allows_everything():
    """``allows`` is the only reader of the allowlist, and "empty means all"
    is the branch a naive ``in`` check would get exactly backwards."""
    open_config = config()

    assert open_config.space_allowlist == frozenset()
    assert open_config.allows("spaces/anything") is True
    assert open_config.allows("") is True


def test_a_populated_allowlist_admits_only_its_members():
    restricted = config(space_allowlist=frozenset({"spaces/a"}))

    assert restricted.allows("spaces/a") is True
    assert restricted.allows("spaces/b") is False


def test_from_env_defaults_the_api_base_and_the_limits():
    parsed = MeetConfig.from_env({"MEET_ACCESS_TOKEN": TOKEN})

    assert parsed.api_base == DEFAULT_API_BASE
    assert parsed.api_base.startswith("https://")
    assert parsed.max_age_hours == 24.0
    assert parsed.max_runs_per_poll == 3


def test_from_env_reads_the_numeric_limits_from_the_environment():
    parsed = MeetConfig.from_env(
        {
            "MEET_ACCESS_TOKEN": TOKEN,
            "MEET_MAX_AGE_HOURS": "1.5",
            "MEET_MAX_RUNS_PER_POLL": "1",
        }
    )

    assert parsed.max_age_hours == 1.5
    assert parsed.max_runs_per_poll == 1


@pytest.mark.parametrize(
    "overrides",
    [{"max_runs_per_poll": 0}, {"max_age_hours": 0.0}, {"max_age_hours": -1.0}],
)
def test_a_nonsensical_limit_is_refused_at_construction(overrides):
    """Zero runs per poll or a zero-hour window is a configuration that can
    never do anything; failing at startup beats a poll that silently no-ops."""
    with pytest.raises(ConfigError):
        config(**overrides)


# -- UrllibTransport: the one real network surface --------------------------


def test_the_real_transport_refuses_to_follow_a_redirect():
    """A 3xx must come back as a response, never be followed.

    The stdlib's default redirect handler re-sends every header, including
    ``Authorization``, to whatever host the redirect names. Refusing it means
    the bearer token can only ever reach the host the caller addressed.
    """
    import urllib.error
    import urllib.request

    from meetings.meet import UrllibTransport, _NoRedirect

    transport = UrllibTransport()
    refusers = [h for h in transport.opener.handlers if isinstance(h, _NoRedirect)]
    assert refusers, "the opener must carry the redirect-refusing handler"
    # The stock handler must not still be installed alongside it: urllib asks
    # every HTTPRedirectHandler in turn, and the stock one would follow.
    assert not [
        h
        for h in transport.opener.handlers
        if isinstance(h, urllib.request.HTTPRedirectHandler)
        and not isinstance(h, _NoRedirect)
    ]
    assert (
        refusers[0].redirect_request(
            None, None, 302, "Found", {}, "https://evil.example/steal"
        )
        is None
    )

    opened: list[urllib.request.Request] = []

    def refuse_with_302(request, timeout):
        opened.append(request)
        raise urllib.error.HTTPError(
            request.full_url, 302, "Found", {"Location": "https://evil.example/"}, None
        )

    transport.opener.open = refuse_with_302
    status, _ = transport.get(
        "https://meet.googleapis.com/v2/conferenceRecords",
        {"Authorization": f"Bearer {TOKEN}"},
    )

    assert status == 302
    assert [r.full_url for r in opened] == [
        "https://meet.googleapis.com/v2/conferenceRecords"
    ]


def test_api_base_must_be_an_allowlisted_google_host():
    """The bearer token may only ever be sent to Meet's own API host.

    ``startswith("https://")`` accepted a URL whose *userinfo* was the Google
    host and whose real host was anything at all, and ``MeetClient._get``
    attaches the live OAuth token to it. The allowlist holds for the fakes
    too, which is why every test here names the real host.
    """
    import pytest

    from meetings.config import ConfigError, MeetConfig

    with pytest.raises(ConfigError, match="not a known Google Meet API host"):
        MeetConfig(
            access_token=TOKEN, api_base="https://meet.googleapis.com@evil.example/v2"
        )
    with pytest.raises(ConfigError, match="not a known Google Meet API host"):
        MeetConfig(access_token=TOKEN, api_base="https://evil.example/v2")
    assert MeetConfig(access_token=TOKEN, api_base=BASE).api_base == BASE


# ---------------------------------------------------------------------------
# Pinned to Google's published discovery document
#
# Every assertion below was read out of
# `GET https://meet.googleapis.com/$discovery/rest?version=v2`, revision
# 20260903, on 2026-09-08. The document is public and needs no credential, so
# these can be re-derived without a Workspace tenant -- which is the whole
# reason they are worth writing down rather than trusting to memory.
# ---------------------------------------------------------------------------


def test_every_list_call_asks_for_the_documented_maximum_page():
    """`pageSize` max is 100; the transcripts and entries defaults are 10.

    Sending none meant ten entries a page, and MAX_PAGES=50 then capped a whole
    conference at 500 utterances -- a forty-minute meeting is past that, so an
    ordinary meeting hit the page cap and raised. Loud, but unreadable.
    """
    assert PAGE_SIZE == 100
    meet, transport = client(
        {
            "conferenceRecords?": page("conferenceRecords", [conference()]),
            "/transcripts": page("transcripts", [{"name": TRANSCRIPT}]),
            "/entries": page("transcriptEntries", [entry("p/1", "hello")]),
        }
    )
    meet.transcript_text(CONF)
    assert transport.requests, "the fake was never called"
    for request in transport.requests:
        assert request.query.get("pageSize") == str(PAGE_SIZE), request.url


def test_the_entries_response_key_is_transcript_entries_not_entries():
    """`ListTranscriptEntriesResponse` names the array `transcriptEntries`.

    The collection lives at `.../entries` but the response key does not match
    it -- the single most likely field name to get wrong in this whole client.
    """
    meet, _ = client(
        {
            "/transcripts": page("transcripts", [{"name": TRANSCRIPT}]),
            "/entries": {
                "transcriptEntries": [entry("participants/p1", "the real key")],
                # A parser keying on `entries` would read this decoy instead.
                "entries": [entry("participants/p9", "the wrong key")],
            },
        }
    )
    assert meet.transcript_text(CONF) == "p1: the real key"


def test_a_missing_end_time_is_googles_own_signal_for_an_ongoing_conference():
    """ConferenceRecord.endTime: "Unset if the conference is ongoing."."""
    meet, _ = client(
        {
            "conferenceRecords?": page(
                "conferenceRecords",
                [conference(name="conferenceRecords/live", end_time=None)],
            )
        }
    )
    assert [c.ongoing for c in meet.conferences()] == [True]
    assert meet.recent_conferences() == []


def test_a_transcript_still_being_written_is_skipped_and_said_out_loud():
    """Transcript.state STARTED: "An active transcript session has started".

    Its entries are half a meeting. Refused with a named MeetError rather than
    returned as "", because run_conference reads "" as "the organiser never
    turned transcription on" -- a false diagnosis pointing at the wrong fix.
    """
    meet, _ = client(
        {
            "/transcripts": page(
                "transcripts", [{"name": TRANSCRIPT, "state": "STARTED"}]
            ),
            "/entries": page("transcriptEntries", [entry("p/1", "half a sentence")]),
        }
    )
    with pytest.raises(MeetError) as caught:
        meet.transcript_text(CONF)
    assert "still being written" in str(caught.value)
    assert "STARTED" in str(caught.value)


def test_a_finished_transcript_is_read_whatever_its_terminal_state():
    """ENDED and FILE_GENERATED are both final; only STARTED is not."""
    for state in ("ENDED", "FILE_GENERATED", ""):
        meet, _ = client(
            {
                "/transcripts": page(
                    "transcripts", [{"name": TRANSCRIPT, "state": state}]
                ),
                "/entries": page(
            "transcriptEntries", [entry("participants/p1", "all of it")]
        ),
            }
        )
        assert meet.transcript_text(CONF) == "p1: all of it", state


def test_a_mix_of_written_and_finished_transcripts_reads_the_finished_one():
    """One unfinished transcript must not suppress a finished one beside it."""
    meet, _ = client(
        {
            "/transcripts": page(
                "transcripts",
                [
                    {"name": f"{CONF}/transcripts/live", "state": "STARTED"},
                    {"name": TRANSCRIPT, "state": "FILE_GENERATED"},
                ],
            ),
            "/entries": page(
                "transcriptEntries",
                [entry("participants/p1", "the finished half")],
            ),
        }
    )
    assert meet.transcript_text(CONF) == "p1: the finished half"

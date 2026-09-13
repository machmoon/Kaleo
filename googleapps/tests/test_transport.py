"""The host allowlist: nothing leaves toward a host we do not know."""

from __future__ import annotations

import pytest

from googleapps.transport import GoogleError, ensure_google_url, mask


@pytest.mark.parametrize(
    "url",
    [
        "https://oauth2.googleapis.com/token",
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        "https://www.googleapis.com/calendar/v3/calendars/primary/events",
        "https://chat.googleapis.com/v1/spaces/A/messages?key=k",
    ],
)
def test_every_endpoint_this_package_uses_is_allowed(url):
    assert ensure_google_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/collect",
        "https://chat.googleapis.com.evil.example/v1/spaces/A",  # suffix trick
        "https://api.slack.com/api/chat.postMessage",
        "http://chat.googleapis.com/v1/spaces/A",  # https only
        "https://accounts.google.com/o/oauth2/v2/auth",  # browser-only, never POSTed
        "ftp://oauth2.googleapis.com/token",
    ],
)
def test_everything_else_is_refused_before_any_transport_runs(url):
    with pytest.raises(GoogleError, match="bad_host"):
        ensure_google_url(url)


def test_mask_shows_only_a_tail():
    assert mask("ya29.a-very-long-secret-token") == "…oken"
    assert mask("short") == "<set>"
    assert mask("") == "<unset>"


def test_redirects_are_refused_so_a_bearer_token_cannot_follow_one():
    """urllib would re-send the request, Authorization header included, to
    whatever host a 302 names; the transport's handler refuses instead."""
    import urllib.request

    from googleapps.transport import _NoRedirect

    handler = _NoRedirect()
    req = urllib.request.Request(
        "https://gmail.googleapis.com/x", headers={"Authorization": "Bearer t"}
    )
    assert handler.redirect_request(
        req, None, 302, "Found", {}, "https://evil.example/collect"
    ) is None

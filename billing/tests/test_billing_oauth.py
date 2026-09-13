"""The Stripe consent flow, driven end to end with no browser and no network.

Every response body here is the shape Stripe actually returned when the
endpoints were read live on 2026-09-06 -- the discovery documents are copied
verbatim, minus fields this code does not read.
"""

from __future__ import annotations

import json
import stat
import urllib.parse

import pytest

from billing import oauth
from billing.errors import StripeError
from billing.oauth import OAuthError
from billing.transport import HttpResponse

RESOURCE_DOC = {
    "resource": "https://mcp.stripe.com",
    "authorization_servers": ["https://access.stripe.com/mcp"],
}
SERVER_DOC = {
    "issuer": "https://access.stripe.com/mcp",
    "authorization_endpoint": "https://access.stripe.com/mcp/oauth2/authorize",
    "token_endpoint": "https://access.stripe.com/mcp/oauth2/token",
    "registration_endpoint": "https://access.stripe.com/mcp/oauth2/register",
    "revocation_endpoint": "https://access.stripe.com/mcp/oauth2/revoke",
    "grant_types_supported": ["authorization_code", "refresh_token"],
    "token_endpoint_auth_methods_supported": ["none"],
    "code_challenge_methods_supported": ["S256"],
    "scopes_supported": ["mcp"],
}
REGISTERED = {"client_id": "oacli_TESTONLY0000", "token_endpoint_auth_method": "none"}
TOKEN = {
    "access_token": "stripe_mcp_at_TESTONLY",
    "refresh_token": "stripe_mcp_rt_TESTONLY",
    "expires_in": 3600,
    "scope": "mcp",
    "token_type": "Bearer",
}


class FakeStripe:
    """Routes by URL instead of by call order, so a test that changes the
    number of calls does not silently start asserting against the wrong
    response."""

    def __init__(self, **overrides):
        self.routes = {
            oauth.PROTECTED_RESOURCE_URL: (200, RESOURCE_DOC),
            "https://access.stripe.com/.well-known/"
            "oauth-authorization-server/mcp": (200, SERVER_DOC),
            SERVER_DOC["registration_endpoint"]: (200, REGISTERED),
            SERVER_DOC["token_endpoint"]: (200, TOKEN),
            SERVER_DOC["revocation_endpoint"]: (200, {}),
        }
        self.routes.update(overrides)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        url = request.url.split("?")[0]
        status, payload = self.routes.get(url, (404, {"error": "not_found"}))
        return HttpResponse(status=status, body=json.dumps(payload).encode())

    def form_for(self, url):
        for request in reversed(self.requests):
            if request.url.split("?")[0] == url:
                return dict(urllib.parse.parse_qsl((request.body or b"").decode()))
        raise AssertionError(f"nothing was posted to {url}")


def fake_authorize(state_holder):
    """Stand in for the browser: read the state out of the URL Kaleo built
    and hand back the redirect Stripe would have sent."""

    def authorize(build_url):
        url = build_url("http://127.0.0.1:53682/callback")
        state_holder["url"] = url
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        return f"/callback?code=ac_TESTONLY&state={query['state'][0]}"

    return authorize


# -- discovery -------------------------------------------------------------


def test_discovery_finds_the_three_endpoints_stripe_publishes():
    server = oauth.discover(FakeStripe())
    assert server.issuer == "https://access.stripe.com/mcp"
    assert server.token_endpoint == SERVER_DOC["token_endpoint"]
    assert server.registration_endpoint == SERVER_DOC["registration_endpoint"]


def test_metadata_is_looked_for_at_the_rfc_8414_path_first():
    """Path insertion, not suffix. Stripe 404s the suffix form, so getting
    this order wrong makes discovery depend on a fallback that costs a round
    trip and a 404 every single time."""
    first, second = oauth._metadata_urls("https://access.stripe.com/mcp")
    assert first == (
        "https://access.stripe.com/.well-known/oauth-authorization-server/mcp"
    )
    assert second == (
        "https://access.stripe.com/mcp/.well-known/oauth-authorization-server"
    )


def test_the_suffix_form_is_tried_when_the_spec_path_is_missing():
    stripe = FakeStripe(
        **{
            "https://access.stripe.com/.well-known/"
            "oauth-authorization-server/mcp": (404, {}),
            "https://access.stripe.com/mcp/.well-known/"
            "oauth-authorization-server": (200, SERVER_DOC),
        }
    )
    assert oauth.discover(stripe).token_endpoint == SERVER_DOC["token_endpoint"]


def test_a_discovery_document_pointing_off_stripe_is_refused():
    """The whole reason the allowlist is re-applied to discovered URLs: this
    document would otherwise send the PKCE verifier to an attacker and get a
    token back."""
    evil = dict(SERVER_DOC, token_endpoint="https://evil.example/token")
    stripe = FakeStripe(
        **{
            "https://access.stripe.com/.well-known/"
            "oauth-authorization-server/mcp": (200, evil)
        }
    )
    with pytest.raises(StripeError, match="non-Stripe host"):
        oauth.discover(stripe)


def test_an_authorization_server_off_stripe_is_refused():
    doc = {"authorization_servers": ["https://evil.example/as"]}
    with pytest.raises(StripeError, match="non-Stripe host"):
        oauth.discover(FakeStripe(**{oauth.PROTECTED_RESOURCE_URL: (200, doc)}))


def test_metadata_declaring_a_different_issuer_is_refused():
    doc = dict(SERVER_DOC, issuer="https://access.stripe.com/somewhere-else")
    stripe = FakeStripe(
        **{
            "https://access.stripe.com/.well-known/"
            "oauth-authorization-server/mcp": (200, doc)
        }
    )
    with pytest.raises(OAuthError, match="different issuer"):
        oauth.discover(stripe)


def test_losing_pkce_s256_is_refused_rather_than_downgraded():
    doc = dict(SERVER_DOC, code_challenge_methods_supported=["plain"])
    stripe = FakeStripe(
        **{
            "https://access.stripe.com/.well-known/"
            "oauth-authorization-server/mcp": (200, doc)
        }
    )
    with pytest.raises(OAuthError, match="S256"):
        oauth.discover(stripe)


# -- registration ----------------------------------------------------------


def test_registration_asks_for_a_public_client_with_no_secret():
    stripe = FakeStripe()
    server = oauth.discover(stripe)
    client = oauth.register_client(
        stripe, server, redirect_uri="http://127.0.0.1:53682/callback"
    )
    assert client.client_id == "oacli_TESTONLY0000"
    sent = json.loads(stripe.requests[-1].body)
    assert sent["token_endpoint_auth_method"] == "none"
    assert sent["redirect_uris"] == ["http://127.0.0.1:53682/callback"]
    assert "refresh_token" in sent["grant_types"]


def test_a_client_secret_is_refused_rather_than_written_to_disk():
    """A desktop app cannot keep a secret. If Stripe ever starts issuing one
    the right answer is a loud failure, not a 0o600 file pretending."""
    stripe = FakeStripe(
        **{
            SERVER_DOC["registration_endpoint"]: (
                200,
                {"client_id": "oacli_x", "client_secret": "shhh"},
            )
        }
    )
    server = oauth.discover(stripe)
    with pytest.raises(OAuthError, match="refuses to store"):
        oauth.register_client(stripe, server, redirect_uri="http://127.0.0.1:1/cb")


# -- the flow --------------------------------------------------------------


def test_the_whole_flow_writes_a_private_token_and_reports_connected(tmp_path):
    path = tmp_path / "stripe_oauth.json"
    stripe = FakeStripe()
    held = {}
    result = oauth.run_oauth_flow(
        stripe, path=path, authorize=fake_authorize(held), now=1_000.0
    )
    assert result["connected"] is True
    assert result["scope"] == "mcp"
    assert result["expires_at"] == 1_000.0 + 3600
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert oauth.token_status(path, now=1_000.0) == "connected"


def test_the_consent_url_carries_pkce_the_resource_and_the_state(tmp_path):
    held = {}
    oauth.run_oauth_flow(
        FakeStripe(), path=tmp_path / "t.json", authorize=fake_authorize(held)
    )
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(held["url"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["resource"] == ["https://mcp.stripe.com"]
    assert query["scope"] == ["mcp"]
    assert query["redirect_uri"] == ["http://127.0.0.1:53682/callback"]
    assert len(query["code_challenge"][0]) == 43  # SHA-256, base64url, unpadded
    assert len(query["state"][0]) >= 40


def test_the_verifier_reaches_the_token_call_and_never_the_disk(tmp_path):
    path = tmp_path / "t.json"
    stripe = FakeStripe()
    held = {}
    oauth.run_oauth_flow(stripe, path=path, authorize=fake_authorize(held))
    form = stripe.form_for(SERVER_DOC["token_endpoint"])
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == "ac_TESTONLY"
    assert form["resource"] == "https://mcp.stripe.com"
    assert "client_secret" not in form
    verifier = form["code_verifier"]
    assert len(verifier) == 86
    assert verifier not in path.read_text()
    assert verifier not in held["url"]


def test_a_tampered_state_aborts_the_flow(tmp_path):
    def authorize(build_url):
        build_url("http://127.0.0.1:53682/callback")
        return "/callback?code=ac_TESTONLY&state=not-the-one-we-sent"

    with pytest.raises(OAuthError, match="state mismatch"):
        oauth.run_oauth_flow(
            FakeStripe(), path=tmp_path / "t.json", authorize=authorize
        )
    assert not (tmp_path / "t.json").exists()


def test_declining_consent_stores_nothing_and_says_so(tmp_path):
    def authorize(build_url):
        build_url("http://127.0.0.1:53682/callback")
        return "/callback?error=access_denied&error_description=User+said+no"

    with pytest.raises(OAuthError, match="User said no"):
        oauth.run_oauth_flow(
            FakeStripe(), path=tmp_path / "t.json", authorize=authorize
        )
    assert not (tmp_path / "t.json").exists()


def test_a_second_connect_on_the_same_port_reuses_the_registered_client(tmp_path):
    path = tmp_path / "t.json"
    stripe = FakeStripe()
    oauth.run_oauth_flow(stripe, path=path, authorize=fake_authorize({}))
    oauth.run_oauth_flow(stripe, path=path, authorize=fake_authorize({}))
    registrations = [
        r
        for r in stripe.requests
        if r.url == SERVER_DOC["registration_endpoint"]
    ]
    assert len(registrations) == 1


def test_a_different_callback_port_forces_a_fresh_registration(tmp_path):
    """The redirect URI is baked into the registration, so a client stored
    against :53682 is useless once :53683 is what got bound -- Stripe would
    reject the redirect and the user would see a blank failure."""
    path = tmp_path / "t.json"
    stripe = FakeStripe()
    oauth.run_oauth_flow(stripe, path=path, authorize=fake_authorize({}))

    def on_other_port(build_url):
        url = build_url("http://127.0.0.1:53683/callback")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        return f"/cb?code=ac_TESTONLY&state={query['state'][0]}"

    oauth.run_oauth_flow(stripe, path=path, authorize=on_other_port)
    registrations = [
        r
        for r in stripe.requests
        if r.url == SERVER_DOC["registration_endpoint"]
    ]
    assert len(registrations) == 2


# -- refresh and revoke ----------------------------------------------------


def test_an_expired_token_refreshes_transparently_and_persists(tmp_path):
    path = tmp_path / "t.json"
    stripe = FakeStripe()
    oauth.run_oauth_flow(stripe, path=path, authorize=fake_authorize({}), now=0.0)
    stripe.routes[SERVER_DOC["token_endpoint"]] = (
        200,
        {"access_token": "stripe_mcp_at_SECOND", "expires_in": 3600, "scope": "mcp"},
    )
    assert oauth.access_token(stripe, path=path, now=10_000.0) == "stripe_mcp_at_SECOND"
    form = stripe.form_for(SERVER_DOC["token_endpoint"])
    assert form["grant_type"] == "refresh_token"
    assert form["refresh_token"] == "stripe_mcp_rt_TESTONLY"
    # A refresh response with no new refresh token must not lose the old one.
    assert json.loads(path.read_text())["token"]["refresh_token"] == (
        "stripe_mcp_rt_TESTONLY"
    )


def test_a_live_token_is_returned_without_touching_the_network(tmp_path):
    path = tmp_path / "t.json"
    stripe = FakeStripe()
    oauth.run_oauth_flow(stripe, path=path, authorize=fake_authorize({}), now=0.0)
    before = len(stripe.requests)
    assert oauth.access_token(stripe, path=path, now=100.0) == "stripe_mcp_at_TESTONLY"
    assert len(stripe.requests) == before


def test_a_revoked_consent_names_the_one_thing_that_fixes_it(tmp_path):
    path = tmp_path / "t.json"
    stripe = FakeStripe()
    oauth.run_oauth_flow(stripe, path=path, authorize=fake_authorize({}), now=0.0)
    stripe.routes[SERVER_DOC["token_endpoint"]] = (400, {"error": "invalid_grant"})
    with pytest.raises(OAuthError, match="Settings -> Billing"):
        oauth.access_token(stripe, path=path, now=10_000.0)


def test_disconnect_deletes_the_local_token_even_if_stripe_is_unreachable(tmp_path):
    path = tmp_path / "t.json"
    stripe = FakeStripe()
    oauth.run_oauth_flow(stripe, path=path, authorize=fake_authorize({}))

    def broken(request):
        raise StripeError("could not reach Stripe: down")

    assert oauth.revoke(broken, path=path) is False
    assert not path.exists()


def test_disconnect_tells_stripe_when_it_can(tmp_path):
    path = tmp_path / "t.json"
    stripe = FakeStripe()
    oauth.run_oauth_flow(stripe, path=path, authorize=fake_authorize({}))
    assert oauth.revoke(stripe, path=path) is True
    assert not path.exists()
    assert stripe.requests[-1].url == SERVER_DOC["revocation_endpoint"]


# -- persistence and honesty -----------------------------------------------


def test_a_stored_endpoint_pointing_off_stripe_is_refused_on_read(tmp_path):
    """Defence in depth: the file is 0o600, but a URL read back from disk
    still receives a refresh token."""
    path = tmp_path / "t.json"
    oauth.run_oauth_flow(FakeStripe(), path=path, authorize=fake_authorize({}))
    payload = json.loads(path.read_text())
    payload["server"]["token_endpoint"] = "https://evil.example/token"
    path.write_text(json.dumps(payload))
    with pytest.raises(StripeError, match="non-Stripe host"):
        oauth.load_state(path)


def test_describe_does_not_claim_consent_replaces_the_api_key(tmp_path):
    """The user asked for a click-allow popup and Stripe really has one --
    but it grants MCP scope, not an API key. The Settings pane must say so,
    because the previous version of this text asserted the opposite."""
    described = oauth.describe(tmp_path / "absent.json")
    assert described["connected"] is False
    assert described["client_secret_required"] is False
    assert described["pkce"] == "S256"
    assert "api.stripe.com" in described["does_not_cover"]
    assert "restricted key" in described["does_not_cover"]


def test_describe_never_carries_a_token(tmp_path):
    path = tmp_path / "t.json"
    oauth.run_oauth_flow(FakeStripe(), path=path, authorize=fake_authorize({}))
    blob = json.dumps(oauth.describe(path))
    assert "TESTONLY" not in blob
    assert oauth.describe(path)["connected"] is True


def test_the_stored_client_repr_shows_no_token(tmp_path):
    client = oauth.StoredClient("oacli_x", "http://127.0.0.1:53682/callback")
    assert "oacli_x" in repr(client)

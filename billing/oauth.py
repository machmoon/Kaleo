"""Stripe's browser consent flow, so nobody has to paste an API key.

This is the "click allow" path the user asked for, and it is the same shape
as the Google sign-in already in ``googleapps/auth.py``: open a consent page,
catch the redirect on a 127.0.0.1 loopback, exchange the code with PKCE,
write the token 0o600. Two differences, both because of what Stripe
publishes:

* **There is no client secret and no pre-registered client.** Stripe's
  authorization server advertises ``token_endpoint_auth_methods_supported:
  ["none"]`` and a ``registration_endpoint``, so this app registers itself
  once (RFC 7591 Dynamic Client Registration) and stores the ``client_id``
  it gets back. A desktop app cannot keep a secret, and here it does not
  have to.
* **The token addresses MCP, not REST.** The protected resource is
  ``https://mcp.stripe.com``; the scope Stripe grants is ``mcp``. So a token
  from this flow drives Stripe through MCP tool calls, *not* through
  ``api.stripe.com`` with a bearer key. Everything else in this package is
  REST and keeps needing a restricted key. This module is deliberately
  honest about that rather than implying consent replaces key setup --
  :func:`describe` is what the Settings pane renders.

All three endpoints are discovered at runtime (RFC 9728 then RFC 8414)
instead of hard-coded, and every URL that comes back is re-checked against
:data:`billing.transport.OAUTH_HOSTS` before it is used. Metadata from the
network is input, not instruction: without that check a compromised or
spoofed discovery document could point the token exchange -- which carries
the PKCE verifier and returns the access token -- at any host it liked.

Verified live on 2026-09-06::

    GET https://mcp.stripe.com/.well-known/oauth-protected-resource
      -> {"resource": "https://mcp.stripe.com",
          "authorization_servers": ["https://access.stripe.com/mcp"]}
    GET https://access.stripe.com/.well-known/oauth-authorization-server/mcp
      -> code_challenge_methods_supported: ["S256"]
         token_endpoint_auth_methods_supported: ["none"]
         grant_types_supported: ["authorization_code", "refresh_token"]
         scopes_supported: ["mcp"]
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import BillingError
from .transport import (
    OAUTH_HOSTS,
    HttpRequest,
    Transport,
    ensure_stripe_url,
    urllib_transport,
)

__all__ = [
    "CALLBACK_PORTS",
    "PROTECTED_RESOURCE_URL",
    "RESOURCE",
    "SCOPE",
    "AuthServer",
    "OAuthError",
    "StoredClient",
    "TOKEN_PATH",
    "access_token",
    "build_authorize_url",
    "describe",
    "discover",
    "exchange_code",
    "load_state",
    "parse_redirect",
    "pkce_pair",
    "register_client",
    "revoke",
    "run_oauth_flow",
    "save_state",
    "token_status",
]

#: The MCP resource this consent is for. Sent as ``resource`` on both the
#: authorize and token calls (RFC 8707) so the token cannot be replayed at
#: some other audience.
RESOURCE = "https://mcp.stripe.com"
PROTECTED_RESOURCE_URL = f"{RESOURCE}/.well-known/oauth-protected-resource"
SCOPE = "mcp"

#: Fixed loopback ports, tried in order. Google's flow can use port 0 because
#: its client is pre-registered with a wildcard loopback; here the redirect
#: URI is baked into the dynamic registration, so the port has to be known
#: *before* the socket is bound and has to stay stable across runs or the
#: stored ``client_id`` stops matching.
CALLBACK_PORTS = (53682, 53683, 53684)

TOKEN_PATH = Path.home() / ".kaleo" / "stripe_oauth.json"
_DEFAULT_TOKEN_PATH = TOKEN_PATH

#: A token this close to expiry is treated as expired, so a call cannot start
#: with a token that dies mid-flight. Same 60s as ``googleapps/auth.py``.
EXPIRY_SKEW_S = 60

RERUN_HINT = "connect Stripe again from Settings -> Billing"


def _resolve(path: Path | None) -> Path:
    """``TOKEN_PATH`` looked up at call time, not baked into a default.

    A module-level default argument is bound once at import, so the token
    location could never be redirected -- not by a test, and not by an
    install that wants its state somewhere other than ``~/.kaleo``.
    """
    if path is not None:
        return path
    if TOKEN_PATH != _DEFAULT_TOKEN_PATH:
        # Overridden after import (a test, or an install that moved it).
        return TOKEN_PATH
    home = (os.environ.get("KALEO_HOME") or "").strip()
    return Path(home).expanduser() / "stripe_oauth.json" if home else TOKEN_PATH


class OAuthError(BillingError):
    """The consent flow failed. Never carries a token or a verifier."""


# -- discovery -------------------------------------------------------------


@dataclass(frozen=True)
class AuthServer:
    """The three endpoints, each already checked against ``OAUTH_HOSTS``."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str
    revocation_endpoint: str | None = None

    def describe(self) -> dict[str, str]:
        """Exactly what :func:`save_state` persists, so a reconnect and a
        disconnect after a restart both have every endpoint they need. An
        earlier version dropped ``revocation_endpoint`` here, which silently
        turned every disconnect into a local-only delete."""
        described = {
            "issuer": self.issuer,
            "authorization_endpoint": self.authorization_endpoint,
            "token_endpoint": self.token_endpoint,
            "registration_endpoint": self.registration_endpoint,
        }
        if self.revocation_endpoint:
            described["revocation_endpoint"] = self.revocation_endpoint
        return described


def _get_json(transport: Transport, url: str) -> dict[str, Any]:
    response = transport(
        HttpRequest(
            ensure_stripe_url(url, hosts=OAUTH_HOSTS),
            "GET",
            {"Accept": "application/json"},
            None,
            OAUTH_HOSTS,
        )
    )
    if response.status >= 300:
        raise OAuthError(f"Stripe discovery at {url} answered HTTP {response.status}")
    payload = response.json()
    if not isinstance(payload, dict):
        raise OAuthError(f"Stripe discovery at {url} did not return an object")
    return payload


def _checked(metadata: dict[str, Any], key: str, *, required: bool = True) -> str:
    """One URL out of a discovery document, or a refusal.

    The allowlist check is the point of this function. These URLs arrive over
    the network and then receive the PKCE verifier and hand back the access
    token; a document that named ``token_endpoint: https://evil.example/t``
    would otherwise be obeyed.
    """
    raw = metadata.get(key)
    if not isinstance(raw, str) or not raw:
        if required:
            raise OAuthError(f"Stripe's OAuth metadata has no {key}")
        return ""
    return ensure_stripe_url(raw, hosts=OAUTH_HOSTS)


def _metadata_urls(issuer: str) -> list[str]:
    """RFC 8414 path insertion first, then the OIDC-style suffix.

    For issuer ``https://access.stripe.com/mcp`` the spec puts the document
    at ``https://access.stripe.com/.well-known/oauth-authorization-server/mcp``
    -- the well-known segment goes *before* the path, not after. Stripe
    implements exactly that and 404s the naive suffix form, so the order
    here is load-bearing rather than cosmetic.
    """
    parsed = urllib.parse.urlsplit(ensure_stripe_url(issuer, hosts=OAUTH_HOSTS))
    root = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.rstrip("/")
    urls = [f"{root}/.well-known/oauth-authorization-server{path}"]
    if path:
        urls.append(f"{root}{path}/.well-known/oauth-authorization-server")
    return urls


def discover(transport: Transport) -> AuthServer:
    """Find Stripe's authorization server the way an MCP client would."""
    resource = _get_json(transport, PROTECTED_RESOURCE_URL)
    servers = resource.get("authorization_servers")
    if not isinstance(servers, list) or not servers:
        raise OAuthError("Stripe's MCP resource named no authorization server")
    issuer = ensure_stripe_url(str(servers[0]), hosts=OAUTH_HOSTS)

    last: OAuthError | None = None
    for url in _metadata_urls(issuer):
        try:
            metadata = _get_json(transport, url)
        except OAuthError as exc:
            last = exc
            continue
        if str(metadata.get("issuer") or issuer) != issuer:
            raise OAuthError(
                "Stripe's OAuth metadata declares a different issuer than the "
                "one the MCP resource pointed at; refusing it"
            )
        methods = metadata.get("code_challenge_methods_supported") or []
        if "S256" not in methods:
            raise OAuthError(
                "Stripe's authorization server no longer advertises PKCE S256; "
                "refusing to fall back to a weaker method"
            )
        return AuthServer(
            issuer=issuer,
            authorization_endpoint=_checked(metadata, "authorization_endpoint"),
            token_endpoint=_checked(metadata, "token_endpoint"),
            registration_endpoint=_checked(metadata, "registration_endpoint"),
            revocation_endpoint=(
                _checked(metadata, "revocation_endpoint", required=False) or None
            ),
        )
    raise OAuthError(
        f"could not read Stripe's OAuth metadata for {issuer}"
        + (f": {last}" if last else "")
    )


# -- dynamic client registration -------------------------------------------


@dataclass(frozen=True)
class StoredClient:
    client_id: str
    redirect_uri: str

    def __repr__(self) -> str:
        return f"StoredClient({self.client_id!r} -> {self.redirect_uri!r})"


def register_client(
    transport: Transport, server: AuthServer, *, redirect_uri: str
) -> StoredClient:
    """Register this install as a public native client (RFC 7591).

    No secret is requested and none is stored: ``token_endpoint_auth_method``
    is ``none``, which is what Stripe advertises and what a desktop app can
    actually honour. PKCE is the proof, not a shipped credential.
    """
    body = json.dumps(
        {
            "client_name": "Hardy",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "application_type": "native",
            "scope": SCOPE,
        }
    ).encode("utf-8")
    response = transport(
        HttpRequest(
            server.registration_endpoint,
            "POST",
            {"Content-Type": "application/json", "Accept": "application/json"},
            body,
            OAUTH_HOSTS,
        )
    )
    payload = response.json() if response.body else {}
    if response.status >= 300 or not isinstance(payload, dict):
        raise OAuthError(
            f"Stripe refused the client registration (HTTP {response.status})"
        )
    client_id = str(payload.get("client_id") or "")
    if not client_id:
        raise OAuthError("Stripe's client registration returned no client_id")
    if payload.get("client_secret"):
        # Stripe advertises auth method "none". If that ever changes, fail
        # loudly rather than quietly writing a secret to disk in a desktop
        # app, which is the exact thing this flow exists to avoid.
        raise OAuthError(
            "Stripe issued a client secret; this desktop flow refuses to "
            "store one -- report this, the integration needs rethinking"
        )
    return StoredClient(client_id=client_id, redirect_uri=redirect_uri)


# -- PKCE ------------------------------------------------------------------


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def pkce_pair() -> tuple[str, str]:
    """A fresh ``(verifier, challenge)``, S256. The verifier never leaves
    this process's memory and is never written to disk."""
    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def build_authorize_url(
    server: AuthServer, client: StoredClient, *, challenge: str, state: str
) -> str:
    query = urllib.parse.urlencode(
        {
            "client_id": client.client_id,
            "redirect_uri": client.redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "resource": RESOURCE,
        }
    )
    return f"{server.authorization_endpoint}?{query}"


def parse_redirect(path: str, expected_state: str) -> str:
    """The code out of the loopback redirect, or a refusal. Every failure
    mode raises: a state mismatch is CSRF or a stale tab, and ``error`` is
    the user declining -- neither may continue the flow."""
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
    if "error" in query:
        detail = (query.get("error_description") or query["error"])[0]
        raise OAuthError(f"Stripe refused authorization: {detail}")
    state = (query.get("state") or [""])[0]
    if not state or not secrets.compare_digest(state, expected_state):
        raise OAuthError("state mismatch on the OAuth redirect; aborting the flow")
    code = (query.get("code") or [""])[0]
    if not code:
        raise OAuthError("the OAuth redirect carried no authorization code")
    return code


# -- token endpoint --------------------------------------------------------


def _token_post(
    transport: Transport, server: AuthServer, form: dict[str, str]
) -> dict[str, Any]:
    response = transport(
        HttpRequest(
            server.token_endpoint,
            "POST",
            {
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            urllib.parse.urlencode(form).encode("ascii"),
            OAUTH_HOSTS,
        )
    )
    payload = response.json() if response.body else {}
    if not isinstance(payload, dict):
        raise OAuthError("Stripe's token endpoint did not return an object")
    if response.status >= 300 or "error" in payload:
        code = str(payload.get("error") or f"http_{response.status}")
        if code == "invalid_grant" and form.get("grant_type") == "refresh_token":
            raise OAuthError(f"the stored Stripe consent was revoked; {RERUN_HINT}")
        if code == "invalid_grant":
            raise OAuthError(
                "Stripe refused the authorization code; it may have expired "
                f"or been used already -- {RERUN_HINT}"
            )
        raise OAuthError(f"Stripe's token endpoint refused the request: {code}")
    if not payload.get("access_token"):
        raise OAuthError("Stripe's token endpoint returned no access token")
    return payload


def _stamp(token: dict[str, Any], now: float) -> dict[str, Any]:
    """``expires_in`` (relative, from Stripe) -> ``expires_at`` (absolute,
    ours), the only form a later process can act on."""
    stamped = dict(token)
    stamped["expires_at"] = now + float(stamped.pop("expires_in", 0) or 0)
    return stamped


# -- persistence -----------------------------------------------------------


def save_state(
    path: Path,
    *,
    server: AuthServer,
    client: StoredClient,
    token: dict[str, Any],
) -> None:
    """Write client + token 0o600, atomically.

    Same rule as ``googleapps.auth.save_token``: a fresh sibling created
    0o600, then ``os.replace``, so a rewrite over a file whose mode had been
    loosened never exposes the new token for an instant, and a crash mid-write
    leaves the old file intact rather than a truncated one.
    """
    path = _resolve(path)
    payload = {
        "server": server.describe(),
        "client_id": client.client_id,
        "redirect_uri": client.redirect_uri,
        "token": token,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, indent=2)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    os.chmod(path, 0o600)


def load_state(
    path: Path | None = None,
) -> tuple[AuthServer, StoredClient, dict[str, Any]]:
    path = _resolve(path)
    if not path.exists():
        raise OAuthError(f"Stripe is not connected on this machine; {RERUN_HINT}")
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise OAuthError(f"unreadable Stripe consent at {path}; {RERUN_HINT}") from exc
    if not isinstance(payload, dict):
        raise OAuthError(f"malformed Stripe consent at {path}; {RERUN_HINT}")
    stored = payload.get("server")
    token = payload.get("token")
    if not isinstance(stored, dict) or not isinstance(token, dict):
        raise OAuthError(f"malformed Stripe consent at {path}; {RERUN_HINT}")
    if not token.get("access_token"):
        raise OAuthError(f"malformed Stripe consent at {path}; {RERUN_HINT}")
    # Re-check on the way *out* too. The file is 0o600, but a URL read back
    # from disk is still a URL that will receive a refresh token, and the
    # cost of checking twice is nothing.
    server = AuthServer(
        issuer=ensure_stripe_url(str(stored.get("issuer", "")), hosts=OAUTH_HOSTS),
        authorization_endpoint=_checked(stored, "authorization_endpoint"),
        token_endpoint=_checked(stored, "token_endpoint"),
        registration_endpoint=_checked(stored, "registration_endpoint"),
        revocation_endpoint=(
            _checked(stored, "revocation_endpoint", required=False) or None
        ),
    )
    client = StoredClient(
        client_id=str(payload.get("client_id") or ""),
        redirect_uri=str(payload.get("redirect_uri") or ""),
    )
    if not client.client_id:
        raise OAuthError(f"malformed Stripe consent at {path}; {RERUN_HINT}")
    return server, client, token


def token_status(path: Path | None = None, now: float | None = None) -> str:
    """``missing`` / ``expired`` / ``connected`` -- purely local, no network."""
    path = _resolve(path)
    try:
        _, _, token = load_state(path)
    except OAuthError:
        return "missing"
    current = time.time() if now is None else now
    if float(token.get("expires_at", 0)) <= current + EXPIRY_SKEW_S:
        return "expired (will refresh on next use)"
    return "connected"


def token_file_is_private(path: Path | None = None) -> bool:
    path = _resolve(path)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return False
    if os.name == "nt":
        return True
    return mode == 0o600


# -- the exchange, the refresh, the flow ------------------------------------


def exchange_code(
    transport: Transport,
    server: AuthServer,
    client: StoredClient,
    *,
    code: str,
    verifier: str,
    now: float | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    path = _resolve(path)
    payload = _token_post(
        transport,
        server,
        {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "client_id": client.client_id,
            "redirect_uri": client.redirect_uri,
            "resource": RESOURCE,
        },
    )
    token = _stamp(payload, time.time() if now is None else now)
    save_state(path, server=server, client=client, token=token)
    return token


def _refresh(
    transport: Transport,
    server: AuthServer,
    client: StoredClient,
    token: dict[str, Any],
    now: float,
    path: Path,
) -> dict[str, Any]:
    refresh = str(token.get("refresh_token") or "")
    if not refresh:
        raise OAuthError(f"the stored Stripe consent cannot be refreshed; {RERUN_HINT}")
    payload = _token_post(
        transport,
        server,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": client.client_id,
            "resource": RESOURCE,
        },
    )
    # Keep the old refresh token when the response omits one; drop it when a
    # new one is issued (rotation), which is what a public client should do.
    merged = {**token, **_stamp(payload, now)}
    merged.setdefault("refresh_token", refresh)
    save_state(path, server=server, client=client, token=merged)
    return merged


def access_token(
    transport: Transport | None = None,
    *,
    path: Path | None = None,
    now: float | None = None,
) -> str:
    """A currently-valid MCP access token, refreshing and persisting if needed."""
    path = _resolve(path)
    send = transport or urllib_transport
    current = time.time() if now is None else now
    server, client, token = load_state(path)
    if float(token.get("expires_at", 0)) <= current + EXPIRY_SKEW_S:
        token = _refresh(send, server, client, token, current, path)
    return str(token["access_token"])


def revoke(transport: Transport | None = None, *, path: Path | None = None) -> bool:
    """Tell Stripe to drop the grant, then delete the local file.

    The local file goes even if the network call fails: a user who clicked
    "disconnect" must not be left with a usable token on disk because Stripe
    was unreachable. They can also revoke from the Stripe Dashboard under
    user settings -> OAuth sessions.
    """
    path = _resolve(path)
    send = transport or urllib_transport
    told = False
    try:
        server, client, token = load_state(path)
    except OAuthError:
        return False
    if server.revocation_endpoint:
        try:
            send(
                HttpRequest(
                    server.revocation_endpoint,
                    "POST",
                    {"Content-Type": "application/x-www-form-urlencoded"},
                    urllib.parse.urlencode(
                        {
                            "token": str(token.get("access_token") or ""),
                            "client_id": client.client_id,
                        }
                    ).encode("ascii"),
                    OAUTH_HOSTS,
                )
            )
            told = True
        except Exception:
            told = False
    path.unlink(missing_ok=True)
    return told


# -- browser + loopback -----------------------------------------------------

_LANDING_OK = (
    b"<!doctype html><meta charset='utf-8'><title>Hardy</title>"
    b"<p>Stripe connected. You can close this tab and return to Hardy.</p>"
)
_LANDING_DENIED = (
    b"<!doctype html><meta charset='utf-8'><title>Hardy</title>"
    b"<p>Stripe was not connected. Nothing was stored; return to Hardy and "
    b"try again.</p>"
)


def _landing_for(path: str) -> bytes:
    """What the tab says. It must not claim a connection the redirect itself
    says was refused."""
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
    return _LANDING_DENIED if "error" in query else _LANDING_OK


def _open_consent_url(url: str) -> bool:
    """``webbrowser.open`` often returns False off the main thread even on a
    desktop Mac, so fall back to the platform opener."""
    try:
        if webbrowser.open(url):
            return True
    except Exception:
        pass
    opener = {"darwin": "open"}.get(sys.platform)
    if opener is None and sys.platform.startswith("linux"):
        opener = "xdg-open"
    if opener is None:
        return False
    try:
        return (
            subprocess.run([opener, url], check=False, capture_output=True).returncode
            == 0
        )
    except OSError:
        return False


def _bind_callback(ports: tuple[int, ...], handler: Any) -> http.server.HTTPServer:
    """First free port from ``ports``. The port is part of the registered
    redirect URI, so it cannot be chosen by the kernel."""
    last: OSError | None = None
    for port in ports:
        try:
            return http.server.HTTPServer(("127.0.0.1", port), handler)
        except OSError as exc:
            last = exc
    raise OAuthError(
        "none of Hardy's Stripe sign-in ports "
        f"({', '.join(str(p) for p in ports)}) were free"
        + (f": {last}" if last else "")
    ) from last


def _loopback_authorize(
    ports: tuple[int, ...],
    *,
    open_browser: Callable[[str], Any] | None = None,
    on_url: Callable[[str], None] | None = None,
    timeout_s: float = 300.0,
) -> Callable[[Callable[[str], str]], str]:
    """Browser out, loopback redirect back in.

    Returns ``authorize(build_url) -> redirect path``. Split out so
    :func:`run_oauth_flow` can be driven end to end by tests with a fake
    authorizer, leaving everything except the browser and the socket as the
    code that runs in production.
    """

    def authorize(build_url: Callable[[str], str]) -> str:
        captured: dict[str, str] = {}
        done = threading.Event()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server's spelling
                captured["path"] = self.path
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(_landing_for(self.path))
                done.set()

            def log_message(self, *args: Any) -> None:
                """Silenced: the redirect URL carries the authorization code
                and must not reach stderr."""

        with _bind_callback(ports, Handler) as server:
            server.timeout = 1.0
            url = build_url(f"http://127.0.0.1:{server.server_address[1]}/callback")
            if on_url is not None:
                on_url(url)
            if open_browser is not None:
                open_browser(url)
            elif not _open_consent_url(url) and on_url is None:
                raise OAuthError(
                    "could not open a browser for Stripe sign-in; open this "
                    f"URL yourself: {url}"
                )
            deadline = time.monotonic() + timeout_s
            while not done.is_set():
                if time.monotonic() > deadline:
                    raise OAuthError("timed out waiting for the Stripe redirect")
                server.handle_request()
        return captured.get("path", "")

    return authorize


def run_oauth_flow(
    transport: Transport | None = None,
    *,
    path: Path | None = None,
    ports: tuple[int, ...] = CALLBACK_PORTS,
    authorize: Callable[[Callable[[str], str]], str] | None = None,
    open_browser: Callable[[str], Any] | None = None,
    on_url: Callable[[str], None] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Discover, register if needed, consent, exchange, persist.

    Returns a small status dict -- never the token. The verifier and the
    state exist only inside this call frame.
    """
    path = _resolve(path)
    send = transport or urllib_transport
    server = discover(send)

    reuse: StoredClient | None = None
    try:
        stored_server, stored_client, _ = load_state(path)
        if stored_server.issuer == server.issuer:
            reuse = stored_client
    except OAuthError:
        reuse = None

    verifier, challenge = pkce_pair()
    state = _b64url(secrets.token_bytes(32))
    chosen: dict[str, StoredClient] = {}

    def build_url(redirect_uri: str) -> str:
        # A stored client is only reusable when its redirect URI matches the
        # port we actually bound; otherwise Stripe rejects the redirect and
        # the user sees a blank failure, so re-register instead.
        client = (
            reuse
            if reuse is not None and reuse.redirect_uri == redirect_uri
            else register_client(send, server, redirect_uri=redirect_uri)
        )
        chosen["client"] = client
        return build_authorize_url(server, client, challenge=challenge, state=state)

    redirect_path = (
        authorize
        or _loopback_authorize(ports, open_browser=open_browser, on_url=on_url)
    )(build_url)
    client = chosen["client"]
    code = parse_redirect(redirect_path, state)
    token = exchange_code(
        send, server, client, code=code, verifier=verifier, now=now, path=path
    )
    return {
        "connected": True,
        "issuer": server.issuer,
        "resource": RESOURCE,
        "scope": str(token.get("scope") or SCOPE),
        "client_id": client.client_id,
        "expires_at": token.get("expires_at"),
        "token_path": str(path),
        "private": token_file_is_private(path),
    }


def port_is_free(port: int) -> bool:
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def describe(path: Path | None = None) -> dict[str, Any]:
    """What the Settings pane renders. No network, no secrets.

    The ``covers`` / ``does_not_cover`` split is the honest part: consent is
    real and it is one click, but it authorizes MCP, so it does not remove
    the need for a restricted key on the REST paths this package uses.
    """
    path = _resolve(path)
    status = token_status(path)
    return {
        "status": status,
        "connected": status == "connected",
        "token_path": str(path),
        "private": token_file_is_private(path),
        "resource": RESOURCE,
        "issuer": "https://access.stripe.com/mcp",
        "scope": SCOPE,
        "client_secret_required": False,
        "pkce": "S256",
        "ports": list(CALLBACK_PORTS),
        "covers": (
            "signing in to your own Stripe account from the browser, with no "
            "key to copy and paste, and revoking it later from the Stripe "
            "Dashboard under user settings -> OAuth sessions"
        ),
        "does_not_cover": (
            "the REST calls this app makes to api.stripe.com. Stripe grants "
            "this consent the 'mcp' scope for https://mcp.stripe.com, not an "
            "API key, so a restricted key is still what Checkout, webhooks "
            "and overage charges run on."
        ),
    }

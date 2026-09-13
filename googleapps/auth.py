"""OAuth 2.0 installed-app flow with PKCE, on the stdlib.

``python -m googleapps auth`` (and Ada's ``POST /deliver/auth``) opens
Google's consent page in the browser, catches the redirect on a 127.0.0.1
loopback port, exchanges the code, and writes the token JSON to the token
path with mode 0o600. After that, every Gmail and Calendar call goes through
:func:`access_token`, which refreshes transparently when the stored token has
expired and persists what came back.

Security rules, all load-bearing:

- PKCE is S256. The ``code_verifier`` lives only in this process's memory for
  the duration of the flow -- it is never written to disk and never logged.
- The token file is chmod 0o600, on create *and* on every rewrite.
- Tokens are never printed. Errors that mention the token talk about the
  file, not the contents.
- A missing or revoked token raises :class:`AuthError` telling the user the
  one command that fixes it, rather than a bare HTTP 401.
- **The granted scopes are recorded and believed, not assumed.** Under
  Google's granular consent every scope on the consent screen is its own
  checkbox, so a user can grant Calendar and decline Gmail and still land on
  a successful redirect. The token response's ``scope`` field is what Google
  actually granted; it is persisted beside the token and re-read on refresh
  (a refresh response may omit ``scope``, which per RFC 6749 §5.1 means
  unchanged -- the record is kept, never blanked). Everything that claims a
  destination is usable reads that record, so a declined checkbox is a
  refusal before the pipeline spends a model call rather than an HTTP 403
  after the board already exists.

The consent URL is on ``accounts.google.com``, which is deliberately absent
from the transport allowlist: it is only ever *opened in the user's browser*,
never addressed by this package's own HTTP client. The one token endpoint we
POST to is ``oauth2.googleapis.com``.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import Config
from .transport import GoogleError, HttpRequest, Transport, ensure_google_url

__all__ = [
    "AUTH_URL",
    "CALENDAR_SCOPE",
    "DESTINATION_SCOPES",
    "GMAIL_SCOPE",
    "SCOPE_LABELS",
    "TOKEN_URL",
    "SCOPES",
    "AuthError",
    "access_token",
    "build_auth_url",
    "denied_scopes",
    "destination_state",
    "exchange_code",
    "granted_scopes",
    "load_token",
    "parse_redirect",
    "pkce_pair",
    "require_scopes",
    "run_auth_flow",
    "save_token",
    "scope_hint",
    "scope_state",
    "token_file_is_private",
    "token_scopes",
    "token_status",
]

# Every parameter below was checked on 2026-09-08 against Google's own
# open-source installed-app client -- ``google-auth-oauthlib``, now at
# ``googleapis/google-cloud-python`` in
# ``packages/google-auth-oauthlib/google_auth_oauthlib/flow.py`` -- rather than
# against a doc summary. What matched, function by function:
#
# * ``Flow.authorization_url`` sets ``access_type`` "offline" by default, then
#   ``code_challenge`` = base64url(sha256(verifier)) with the padding stripped
#   (``b64_challenge.decode().split("=")[0]``) and ``code_challenge_method``
#   "S256" -- the same S256 pair :func:`pkce_pair` builds. Its verifier is 128
#   characters drawn from ``ascii_letters + digits + "-._~"``; ours is 86
#   characters of base64url, a subset of the same RFC 7636 unreserved set and
#   inside the same 43..128 range.
# * ``Flow.fetch_token`` sends ``client_secret`` *and* ``code_verifier``. A
#   Google "Desktop app" client has a secret and is expected to present it,
#   which is why :func:`exchange_code` sends both and why PKCE is an addition
#   here rather than a replacement.
# * ``InstalledAppFlow.run_local_server`` binds an ephemeral port and sets
#   ``redirect_uri`` to ``"http://{host}:{port}/"``. This module uses
#   ``http://127.0.0.1:{port}`` -- the loopback *IP*, which Google's installed
#   app documentation names alongside ``localhost``, and without the trailing
#   slash, which upstream itself makes optional
#   (``redirect_uri_trailing_slash``). The same string is sent in the consent
#   URL and in the exchange, so the two cannot disagree.
#
# ``prompt=consent`` and ``include_granted_scopes`` are ours, not upstream's;
# the reasons are at the parameters themselves.
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

#: Exactly what the two features need, nothing broader: send mail as the
#: user, and manage events. Neither scope can read the user's mailbox.
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.send"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"
SCOPES = (GMAIL_SCOPE, CALENDAR_SCOPE)

#: Which scope each delivery destination cannot work without. One table, read
#: by the CLI's pre-flight, ``check`` and ``service.deliver.config_report`` --
#: so the refusal, the panel and the roster cannot disagree about whether
#: Gmail is usable. The spec review is a Calendar insert, hence the same
#: scope; it is listed separately because the panel offers it separately.
DESTINATION_SCOPES: dict[str, str] = {
    "gmail": GMAIL_SCOPE,
    "calendar": CALENDAR_SCOPE,
    "spec_review": CALENDAR_SCOPE,
}

#: How a scope is named to a person. The full URL is printed too -- it is what
#: the consent screen's checkbox corresponds to -- but "Send email on your
#: behalf" is what they were looking at when they unticked it.
SCOPE_LABELS: dict[str, str] = {
    GMAIL_SCOPE: "Send email on your behalf (Gmail)",
    CALENDAR_SCOPE: "Create and edit calendar events (Calendar)",
}

#: A token this close to expiry is treated as expired, so a request cannot
#: start with a token that dies mid-flight.
EXPIRY_SKEW_S = 60

RERUN_HINT = (
    "sign in again from Hardy's Send panel, or run `python -m googleapps auth`"
)


class AuthError(RuntimeError):
    """No usable token. The message always names the command that fixes it."""


# -- PKCE and URLs ---------------------------------------------------------


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def pkce_pair() -> tuple[str, str]:
    """A fresh ``(code_verifier, code_challenge)`` pair, S256.

    The verifier is 64 random bytes base64url-encoded -- 86 characters,
    inside RFC 7636's 43..128 -- and must never be persisted anywhere.
    """
    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def build_auth_url(
    client_id: str,
    redirect_uri: str,
    challenge: str,
    state: str,
    scopes: tuple[str, ...] = SCOPES,
) -> str:
    """The consent URL.

    ``scopes`` is a parameter rather than the module constant because this
    flow is the only OAuth acquisition path in the repo, and Meet needs a
    different set (``meetings.space.readonly``). Hard-coding Gmail's scopes
    here is what forced ``meetings/`` to say "the host supplies
    MEET_ACCESS_TOKEN" and leave acquisition unbuilt. Asking for scopes an
    integration does not use is also how it gets refused by an admin reading
    the consent screen, so each caller names exactly its own.
    """
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            # A refresh token only arrives with offline access, and only
            # reliably on a consented prompt: without ``prompt=consent``
            # Google skips the consent screen for an already-authorised app
            # and returns no refresh token, which reads as "it never asked
            # me anything" followed by an unrefreshable token.
            "access_type": "offline",
            "prompt": "consent",
            # Incremental authorisation. This flow is deliberately callable
            # with a narrower ``scopes`` than :data:`SCOPES` (Meet needs its
            # own), and without this a second authorisation would mint a
            # grant covering only the newer set, silently dropping Gmail.
            # With it, Google merges what was already granted into the new
            # token -- so the ``scope`` we record is the union, which is the
            # question every caller actually asks.
            "include_granted_scopes": "true",
        }
    )
    return f"{AUTH_URL}?{query}"


def parse_redirect(path: str, expected_state: str) -> str:
    """The authorization code out of the loopback redirect, or a refusal.

    Every failure mode raises rather than returning an empty code: a state
    mismatch is a CSRF attempt or a stale tab, and an ``error`` parameter is
    the user declining consent -- both must end the flow, not continue it.
    """
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
    if "error" in query:
        raise AuthError(f"Google refused authorization: {query['error'][0]}")
    state = (query.get("state") or [""])[0]
    if not state or not secrets.compare_digest(state, expected_state):
        raise AuthError("state mismatch on the OAuth redirect; aborting the flow")
    code = (query.get("code") or [""])[0]
    if not code:
        raise AuthError("the OAuth redirect carried no authorization code")
    return code


# -- token persistence -----------------------------------------------------


def save_token(path: Path, token: dict[str, Any]) -> None:
    """Write the token JSON with owner-only permissions, atomically.

    The bytes go to a fresh sibling file created 0o600 and are moved into
    place with ``os.replace``: a rewrite over a token file whose mode had
    been loosened never exposes the new token for even an instant, and a
    crash mid-write leaves the old token intact rather than a truncated
    one. The mode is re-asserted after the move for the rewrite case.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(token, handle, indent=2)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    os.chmod(path, 0o600)


def load_token(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise AuthError(f"no token at {path}; {RERUN_HINT}")
    try:
        token = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthError(f"unreadable token at {path}; {RERUN_HINT}") from exc
    if not isinstance(token, dict) or not token.get("access_token"):
        raise AuthError(f"malformed token at {path}; {RERUN_HINT}")
    return token


def token_status(path: Path, now: float | None = None) -> str:
    """``missing`` / ``expired`` / ``valid`` -- purely local, for ``check``."""
    try:
        token = load_token(path)
    except AuthError:
        return "missing"
    current = time.time() if now is None else now
    if float(token.get("expires_at", 0)) <= current + EXPIRY_SKEW_S:
        return "expired (will refresh on next use)"
    return "valid"


def _stamp(token: dict[str, Any], now: float) -> dict[str, Any]:
    """Convert ``expires_in`` (relative, from Google) to ``expires_at``
    (absolute, ours), which is the only form a later process can act on."""
    stamped = dict(token)
    stamped["expires_at"] = now + float(stamped.pop("expires_in", 0))
    return stamped


# -- granted scopes --------------------------------------------------------
#
# Google's granular consent makes every scope on the screen an independent
# checkbox. The redirect looks identical whether the user ticked both boxes
# or one, so the only place the truth exists is the token response's
# ``scope`` field, and the only way a later process can see it is if we write
# it down.

#: Where the granted set is stored in the token file. A list rather than the
#: space-delimited string Google sends, because a list cannot be half-read by
#: a substring test -- ``"calendar.events" in scope_string`` is true for a
#: scope that merely shares a prefix.
SCOPE_KEY = "scopes"


def granted_scopes(token: dict[str, Any]) -> tuple[str, ...] | None:
    """The scopes Google said it granted, or ``None`` when unrecorded.

    ``None`` is a third answer and it matters: a token file written before
    this was recorded says nothing about what was granted, and treating
    silence as "nothing was granted" would refuse a working sign-in. Callers
    must distinguish *denied* from *unknown*.
    """
    raw = token.get(SCOPE_KEY, token.get("scope"))
    if isinstance(raw, str):
        values = raw.split()
    elif isinstance(raw, (list, tuple)):
        values = [str(item) for item in raw]
    else:
        return None
    cleaned = tuple(value.strip() for value in values if str(value).strip())
    return cleaned or None


def token_scopes(path: Path) -> tuple[str, ...] | None:
    """:func:`granted_scopes` for a token on disk; ``None`` if it is missing."""
    try:
        return granted_scopes(load_token(path))
    except AuthError:
        return None


def scope_state(token: dict[str, Any], scope: str) -> str:
    """``granted`` / ``denied`` / ``unknown`` for one scope."""
    recorded = granted_scopes(token)
    if recorded is None:
        return "unknown"
    return "granted" if scope in recorded else "denied"


def denied_scopes(token: dict[str, Any], required: tuple[str, ...]) -> list[str]:
    """The required scopes the record positively says were not granted.

    An unrecorded token yields an empty list on purpose: it is not evidence
    of a refusal, and a hard refusal on no evidence would break every token
    minted before the record existed.
    """
    return [s for s in required if scope_state(token, s) == "denied"]


def destination_state(token: dict[str, Any] | None, destination: str) -> str:
    """``granted`` / ``denied`` / ``unknown`` for a named delivery destination."""
    scope = DESTINATION_SCOPES[destination]
    if token is None:
        return "unknown"
    return scope_state(token, scope)


def scope_hint(scopes: list[str]) -> str:
    """The one sentence a person can act on, naming the boxes to tick."""
    labelled = ", ".join(f"{SCOPE_LABELS.get(s, s)} ({s})" for s in scopes)
    return (
        f"Google sign-in did not grant: {labelled}. Google's consent screen "
        "makes each permission its own checkbox, so this is a box that was "
        f"left unticked rather than a bug; {RERUN_HINT} and tick every box."
    )


def require_scopes(token: dict[str, Any], required: tuple[str, ...]) -> None:
    """Raise :class:`AuthError` when the record says a needed box was unticked.

    This is the pre-flight the repo's rule asks for: refuse before the
    pipeline spends a model call, rather than at the API call after the board
    already exists.
    """
    missing = denied_scopes(token, required)
    if missing:
        raise AuthError(scope_hint(missing))


def _record_scopes(
    token: dict[str, Any],
    payload: dict[str, Any],
    fallback: tuple[str, ...] | None,
) -> dict[str, Any]:
    """Fold the response's ``scope`` into the token we are about to store.

    RFC 6749 §5.1 makes ``scope`` OPTIONAL in a token response and says its
    omission means the granted scope is identical to the scope the client
    requested. So an omission is handled explicitly with ``fallback`` -- the
    requested set on an exchange, the previously recorded set on a refresh --
    and never by blanking the record, which would silently turn a known
    grant into "unknown" on the first refresh.
    """
    granted = str(payload.get("scope") or "").split()
    if granted:
        token[SCOPE_KEY] = granted
    elif fallback:
        token[SCOPE_KEY] = list(fallback)
    else:
        token.pop(SCOPE_KEY, None)
    return token


# -- token endpoint --------------------------------------------------------


def _token_post(transport: Transport, form: dict[str, str]) -> dict[str, Any]:
    """POST to the token endpoint and return its JSON, or raise.

    ``invalid_grant`` means different things per grant: on a refresh it is
    a revoked or expired refresh token; on the initial exchange it is a
    spent or stale authorization code, or a PKCE verifier that did not
    match. Both end in the same command, but the diagnosis must be right.
    """
    request = HttpRequest(
        "POST",
        ensure_google_url(TOKEN_URL),
        {"Content-Type": "application/x-www-form-urlencoded"},
        urllib.parse.urlencode(form).encode("ascii"),
    )
    response = transport(request)
    payload = response.json()
    if response.status >= 300 or "error" in payload:
        code = str(payload.get("error") or f"http_{response.status}")
        description = str(payload.get("error_description") or "")
        if code == "invalid_grant" and form.get("grant_type") == "refresh_token":
            # The refresh token was revoked or has expired; only a new
            # consent can mint another one.
            raise AuthError(f"the stored Google token was revoked; {RERUN_HINT}")
        if code == "invalid_grant":
            raise AuthError(
                "Google refused the authorization code"
                + (f" ({description})" if description else "")
                + f"; it may have expired or been used already -- {RERUN_HINT}"
            )
        raise GoogleError(code, description or "token call")
    return payload


def exchange_code(
    transport: Transport,
    config: Config,
    *,
    code: str,
    verifier: str,
    redirect_uri: str,
    now: float | None = None,
    scopes: tuple[str, ...] = SCOPES,
) -> dict[str, Any]:
    """Trade the authorization code for tokens and persist them.

    ``scopes`` is what the consent URL asked for, and is used only as the
    RFC 6749 §5.1 fallback when the response omits ``scope``. What Google
    says it granted always wins over what we asked for.
    """
    payload = _token_post(
        transport,
        {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "client_id": config.client_id,
            "client_secret": config.client_secret,
            "redirect_uri": redirect_uri,
        },
    )
    token = _stamp(payload, time.time() if now is None else now)
    _record_scopes(token, payload, scopes)
    save_token(config.token_path, token)
    return token


def _refresh(
    transport: Transport, config: Config, token: dict[str, Any], now: float
) -> dict[str, Any]:
    refresh = str(token.get("refresh_token") or "")
    if not refresh:
        raise AuthError(f"the stored token cannot be refreshed; {RERUN_HINT}")
    payload = _token_post(
        transport,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": config.client_id,
            "client_secret": config.client_secret,
        },
    )
    # Google does not resend the refresh token on a refresh; keep ours.
    merged = {**token, **_stamp(payload, now)}
    merged.setdefault("refresh_token", refresh)
    # A refresh response usually omits ``scope``; that means unchanged, so
    # the recorded set carries over. When it *is* present it is authoritative
    # -- a scope revoked from the account's permissions page disappears here,
    # and the record must follow rather than keep claiming the old grant.
    _record_scopes(merged, payload, granted_scopes(token))
    save_token(config.token_path, merged)
    return merged


def access_token(
    config: Config,
    transport: Transport,
    *,
    now: float | None = None,
    require: tuple[str, ...] = (),
) -> str:
    """A currently-valid access token, refreshing and persisting if needed.

    ``require`` names the scopes the caller is about to use. A scope the
    record positively says was declined raises here, with the sentence that
    names the checkbox -- an HTTP 403 from Gmail names nothing a person can
    act on. The check runs *after* the refresh, so a scope Google dropped on
    the way through is caught on this call rather than the next one.
    """
    current = time.time() if now is None else now
    token = load_token(config.token_path)
    if float(token.get("expires_at", 0)) <= current + EXPIRY_SKEW_S:
        config.require_oauth()
        token = _refresh(transport, config, token, current)
    if require:
        require_scopes(token, require)
    return str(token["access_token"])


# -- the interactive flow --------------------------------------------------

_LANDING_OK = (
    b"<!doctype html><meta charset='utf-8'><title>Hardy</title>"
    b"<p>Signed in. You can close this tab and return to Hardy.</p>"
)
_LANDING_DENIED = (
    b"<!doctype html><meta charset='utf-8'><title>Hardy</title>"
    b"<p>Authorization was not granted. Nothing was stored; return to Hardy "
    b"and try again.</p>"
)


def _landing_for(path: str) -> bytes:
    """What the browser tab says. It must not claim a sign-in that the
    redirect itself says was refused."""
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
    return _LANDING_DENIED if "error" in query else _LANDING_OK


def _open_consent_url(url: str) -> bool:
    """Open the consent page. Returns True when something claimed to open it.

    ``webbrowser.open`` often returns False from a non-main thread (the
    service's request handler) even on a desktop Mac; fall back to the
    platform opener so the CLI and a same-machine service still work.
    """
    try:
        if webbrowser.open(url):
            return True
    except Exception:
        pass
    if sys.platform == "darwin":
        try:
            return subprocess.run(
                ["open", url], check=False, capture_output=True
            ).returncode == 0
        except OSError:
            return False
    if sys.platform.startswith("linux"):
        try:
            return subprocess.run(
                ["xdg-open", url], check=False, capture_output=True
            ).returncode == 0
        except OSError:
            return False
    return False


def _loopback_authorize(
    open_browser: Callable[[str], Any] | None = None,
    timeout_s: float = 300.0,
    *,
    on_url: Callable[[str], None] | None = None,
) -> Callable[[Callable[[str], str], str], str]:
    """The real user-facing half: browser out, loopback redirect back in.

    Returns an ``authorize(auth_url_template, state) -> redirect path``
    callable. Split out so :func:`run_auth_flow` can be driven end to end by
    tests with a fake authorizer -- everything except the browser and the
    socket is then the code that runs in production.

    ``on_url`` is called with the consent URL as soon as it exists (before
    waiting for the redirect), so a caller like Ada can open it itself when
    the service process cannot.
    """

    def authorize(build_url: Callable[[str], str], state: str) -> str:
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
                """Silence the default stderr access log; the redirect URL
                carries the authorization code and must not be printed."""

        with http.server.HTTPServer(("127.0.0.1", 0), Handler) as server:
            server.timeout = 1.0
            port = server.server_address[1]
            url = build_url(f"http://127.0.0.1:{port}")
            if on_url is not None:
                on_url(url)
            print("Opening the Google consent page in your browser…")
            print("If nothing opens, paste this URL yourself:")
            print(f"  {url}")
            if open_browser is not None:
                open_browser(url)
            else:
                opened = _open_consent_url(url)
                if not opened and on_url is None:
                    raise AuthError(
                        "could not open a browser for Google sign-in; "
                        f"open this URL yourself: {url}"
                    )
            deadline = time.monotonic() + timeout_s
            while not done.is_set():
                if time.monotonic() > deadline:
                    raise AuthError("timed out waiting for the OAuth redirect")
                server.handle_request()
        return captured.get("path", "")

    return authorize


def run_auth_flow(
    config: Config,
    transport: Transport,
    *,
    authorize: Callable[[Callable[[str], str], str], str] | None = None,
    open_browser: Callable[[str], Any] | None = None,
    on_url: Callable[[str], None] | None = None,
    now: float | None = None,
    scopes: tuple[str, ...] = SCOPES,
) -> Path:
    """The whole ``auth`` subcommand: consent, redirect, exchange, persist.

    Returns the token path. The verifier and the state exist only inside this
    call frame. ``on_url`` / ``open_browser`` are forwarded to the default
    loopback authorizer when ``authorize`` is omitted.
    """
    config.require_oauth()
    verifier, challenge = pkce_pair()
    state = _b64url(secrets.token_bytes(32))
    redirect: dict[str, str] = {}

    def build_url(redirect_uri: str) -> str:
        redirect["uri"] = redirect_uri
        return build_auth_url(
            config.client_id, redirect_uri, challenge, state, scopes
        )

    path = (
        authorize
        or _loopback_authorize(open_browser=open_browser, on_url=on_url)
    )(build_url, state)
    code = parse_redirect(path, state)
    exchange_code(
        transport,
        config,
        code=code,
        verifier=verifier,
        redirect_uri=redirect["uri"],
        now=now,
        scopes=scopes,
    )
    return config.token_path


def token_file_is_private(path: Path) -> bool:
    """True when the token file exists and only its owner can read it.

    POSIX mode bits do not exist on Windows (``stat`` reports 0o666 for
    every file), so there the answer is "exists": the file lives under the
    user's profile, which the platform's ACLs already keep private.
    """
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return False
    if os.name == "nt":
        return True
    return mode == 0o600

"""The HTTP surface Microsoft Teams calls.

A stdlib server, like ``slackbot/app.py`` and ``service/app.py``, exposing:

* ``POST /api/calls``    — the Graph calling webhook: call lifecycle
  notifications for a meeting the bot was invited into.
* ``POST /api/messages`` — the Bot Framework activity webhook: a message
  addressed to the bot in a meeting chat, which is where a typed request
  ("silkscreen: a 3v3 LDO board") actually arrives.
* ``GET  /healthz``      — liveness.

Four properties are load-bearing and each of them is a bug this shape exists to
prevent:

1. **Authenticity is proved before the body is parsed.** Both webhooks are
   public URLs; anything on the internet can POST to them. The inbound bearer
   JWT is validated first, against the raw request, and a body is only parsed
   after that succeeds — parsing first would mean acting on the *shape* of a
   forged payload even when its token is rejected. (``slackbot/app.py`` closes
   the same hole with an HMAC.)
2. **Stale tokens are refused.** Expiry, not-before and issued-at are checked
   against the clock with a small skew, so a captured notification cannot be
   replayed tomorrow.
3. **Notification ids are remembered.** Teams retries a notification it does
   not see acknowledged quickly, and a retry that slips through is a second
   *paid* pipeline run for one request. The retry and the original are
   indistinguishable from here, so ids are remembered and repeats are dropped.
   A notification that carries no id gets a deterministic one derived from its
   own content, because "no id" must not mean "always new".
4. **Acknowledge fast, work on a thread.** Teams' callback deadline is seconds
   and a pipeline run is minutes. The 202 promises that the request was
   accepted, never that a board exists.

**Signature verification needs a public key and a crypto backend.** RS256 is
not something the standard library can check, so :func:`verify_notification`
takes a :class:`KeySource` and uses ``cryptography`` to do the maths. Both
absences are *named failures* — ``no_key_source`` and ``crypto_unavailable`` —
and both refuse the request. Neither is ever a quiet accept: an endpoint that
waves a token through because it could not check it is worse than one that is
switched off, since it looks like it is working.

**Unverified live.** This has never run against a live Microsoft tenant from
this repo. Every network boundary is a seam with an offline stand-in, so the
tests prove the parsing, the gates and the refusals — and prove nothing about
Microsoft's live behaviour. Basis for the shape: the calling-bot samples in
``microsoft/BotBuilder-Samples`` (an Entra app with a public ``/api/calls``
endpoint that validates the inbound token) and the documented Graph
``communications/calls`` notification payload.

Run it::

    TEAMS_APP_ID=… TEAMS_APP_SECRET=… TEAMS_TENANT_ID=… python -m teamsbot
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import json
import logging
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol

from .config import DEFAULT_PORT as _DEFAULT_PORT
from .config import Config, ConfigError, load_config

__all__ = [
    "AuthError",
    "Dispatcher",
    "Incoming",
    "KeySource",
    "OpenIdKeys",
    "RunMemory",
    "Runner",
    "StaticKeys",
    "ACCEPTED_ISSUERS",
    "CALLING_ISSUERS",
    "KEY_HOSTS",
    "DEFAULT_PORT",
    "MAX_BODY_BYTES",
    "MISSING_RUN_NOTE",
    "OPENID_CONFIGURATION_URLS",
    "incoming_from_calls",
    "incoming_from_activity",
    "make_handler",
    "make_server",
    "main",
    "verify_notification",
]

log = logging.getLogger("teamsbot.app")

#: Re-exported from :mod:`teamsbot.config`, which owns the port now that
#: ``TEAMS_PORT`` is a declared setting. One definition, because a server that
#: binds a different port than the config reports is a callback that silently
#: never arrives.
DEFAULT_PORT = _DEFAULT_PORT

MAX_BODY_BYTES = 1 << 20
#: How much of an over-length body is read before the connection is closed.
#: Bounded is the property that matters: this happens before authentication, so
#: an unauthenticated client must not be able to buy a handler thread cheaply.
MAX_DRAIN_BYTES = 2 * MAX_BODY_BYTES
#: Socket timeout for one request.
SOCKET_TIMEOUT_S = 15.0
#: Wall-clock ceiling on draining one over-length body. The socket timeout
#: restarts on every read, so on its own it bounds *silence*, not time.
DRAIN_DEADLINE_S = 30.0
#: Clock skew allowed on ``exp``/``nbf``. Microsoft's own guidance for bot
#: token validation is five minutes.
CLOCK_SKEW_S = 300.0
#: A token issued longer ago than this is refused even if it has not expired.
#: Bot Framework tokens are short-lived; a much older one is a replay.
MAX_TOKEN_AGE_S = 3600.0
#: How long a queued run waits for a slot before the meeting is told the bot is
#: busy. Long enough to absorb one run ahead of it, short enough that nobody
#: waits on an answer that will never come.
SLOT_WAIT_S = 240.0
#: Concurrent pipeline runs. One: each is expensive, and a meeting bot that
#: fans out to five is a bill, not a feature.
MAX_CONCURRENT_RUNS = 1

#: Token issuers accepted on the **Bot Framework activity** path
#: (``POST /api/messages``).
#:
#: **CONFIRMED** twice over on 2026-09-08, without a tenant:
#:
#: 1. Microsoft's own SDKs declare exactly this one string.
#:    ``microsoft/botbuilder-python``
#:    ``libraries/botframework-connector/botframework/connector/auth/
#:    authentication_constants.py``::
#:
#:        TO_BOT_FROM_CHANNEL_TOKEN_ISSUER = "https://api.botframework.com"
#:
#:    and ``microsoft/botbuilder-dotnet``
#:    ``libraries/Microsoft.Bot.Connector/Authentication/AuthenticationConstants.cs``::
#:
#:        public const string ToBotFromChannelTokenIssuer = "https://api.botframework.com";
#:
#: 2. Both metadata documents in :data:`OPENID_CONFIGURATION_URLS` were
#:    fetched and each declares ``"issuer": "https://api.botframework.com"``.
#:    That is Microsoft's live published claim, not a doc page about it.
ACCEPTED_ISSUERS = ("https://api.botframework.com",)

#: Token issuers accepted on the **Graph calling** path (``POST /api/calls``).
#:
#: **CORRECTED 2026-09-08.** This path used :data:`ACCEPTED_ISSUERS`, on the
#: reasonable assumption that one issuer covers both webhooks. It does not.
#: Microsoft's own calling sample,
#: ``microsoftgraph/microsoft-graph-comms-samples``
#: ``Samples/Common/Sample.Common/Authentication/AuthenticationProvider.cs``,
#: validates a calling notification against **two**::
#:
#:        // The incoming token should be issued by graph.
#:        var authIssuers = new[]
#:        {
#:            "https://graph.microsoft.com",
#:            "https://api.botframework.com",
#:        };
#:
#: A graph-issued notification was therefore refused ``bad_issuer`` here —
#: closed and noisy, which is the right direction to be wrong in, but
#: ``/api/calls`` could never have worked. The two tuples stay **separate**
#: rather than merged: widening the activity path to accept
#: ``graph.microsoft.com`` would loosen a check for a route that has no use
#: for it, and the point of an issuer allowlist is that it is the narrowest
#: one that works.
#:
#: **Unverifiable without a tenant**, and this is the weakest citation in the
#: file: that sample carries Microsoft's own banner saying it "HAS NOT BEEN
#: TESTED RIGOROUSLY" and is "PURELY FOR DEMONSTRATION PURPOSES ONLY", so it
#: is weaker evidence than the two SDK repos behind :data:`ACCEPTED_ISSUERS`.
#: If it is wrong, the failure is a 401 with ``bad_issuer`` naming the issuer
#: it saw — everything needed to correct this line is in that one log entry.
CALLING_ISSUERS = (
    "https://graph.microsoft.com",
    "https://api.botframework.com",
)

#: OpenID metadata documents that name the signing keys — the Bot Framework
#: channel and the calling (Skype) platform. Fetched, never guessed.
#:
#: **CONFIRMED** on 2026-09-08 by fetching both (they are public, no
#: credential):
#:
#: * ``login.botframework.com`` → ``issuer``
#:   ``https://api.botframework.com``, ``jwks_uri``
#:   ``https://login.botframework.com/v1/.well-known/keys``. Matches
#:   ``ToBotFromChannelOpenIdMetadataUrl`` in both botbuilder SDKs.
#: * ``api.aps.skype.com`` → ``issuer`` ``https://api.botframework.com``,
#:   ``jwks_uri`` ``https://api.aps.skype.com/v1/keys``. Matches the
#:   ``authDomain`` constant in Microsoft's Graph calling sample.
#:
#: One curiosity worth recording so nobody "fixes" it: the skype document
#: advertises ``id_token_signing_alg_values_supported: ["RSA256"]`` — not a
#: real JWA name, presumably a typo for ``RS256`` in Microsoft's own document.
#: Nothing here reads that field (:func:`verify_notification` has its own
#: hardcoded algorithm allowlist), so it is inert; if it were ever trusted as
#: input it would match nothing.
OPENID_CONFIGURATION_URLS = (
    "https://login.botframework.com/v1/.well-known/openidconfiguration",
    "https://api.aps.skype.com/v1/.well-known/openidconfiguration",
)

#: The only hosts a key fetch will ever address. Exact matches — a suffix check
#: would wave through ``login.botframework.com.evil.example``.
#:
#: The first two are **CONFIRMED**: they are the hosts of the ``jwks_uri``
#: values the two documents above actually published when fetched.
#: ``login.microsoftonline.com`` is here for the emulator and skill metadata
#: URLs the botbuilder SDKs name
#: (``ToBotFromEmulatorOpenIdMetadataUrl`` =
#: ``https://login.microsoftonline.com/common/v2.0/.well-known/openid-configuration``)
#: and is unused by :data:`OPENID_CONFIGURATION_URLS` today — an allowlist
#: entry with nothing behind it, kept because adding a host later is the edit
#: most likely to be made in a hurry.
KEY_HOSTS = frozenset(
    {"login.botframework.com", "api.aps.skype.com", "login.microsoftonline.com"}
)

#: What a follow-up gets when the run it refers to is gone. Said out loud
#: rather than guessed at: acting on the *wrong* board is the failure this
#: sentence exists to prevent.
MISSING_RUN_NOTE = (
    "I don't have that run any more — this process restarted since it "
    "finished, and run memory is in-process only. Ask for the board again and "
    "I'll rerun it."
)

#: A follow-up verb refers to an earlier run rather than starting a new one.
FOLLOW_UPS = ("review", "order", "status")


# --------------------------------------------------------------------------
# Authenticity
# --------------------------------------------------------------------------


class AuthError(RuntimeError):
    """An inbound request could not be proved to come from Microsoft.

    ``code`` is the specific reason (``missing_token``, ``bad_audience``,
    ``expired``, ``no_key_source``, ``crypto_unavailable``, …) so a log line
    says which of a dozen different problems happened. The reason never
    reaches the client: the response is a bare 401.
    """

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class KeySource(Protocol):
    """Where a signing key comes from, given the token's ``kid``."""

    def public_key(self, kid: str) -> Any:
        """The public key for ``kid``. Raises :class:`AuthError` if unknown."""
        ...


def _b64url(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(segment + padding)
    except (binascii.Error, ValueError) as exc:
        raise AuthError("malformed_token", "a segment was not base64url") from exc


def _decode_segment(segment: str, what: str) -> dict[str, Any]:
    try:
        payload = json.loads(_b64url(segment).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuthError("malformed_token", f"the {what} was not JSON") from exc
    if not isinstance(payload, dict):
        raise AuthError("malformed_token", f"the {what} was not a JSON object")
    return payload


def bearer_token(headers: Any) -> str:
    """The bearer token out of an ``Authorization`` header, or raise.

    A missing header is the overwhelmingly common case for an unauthenticated
    probe, and it gets its own code so it can be logged at a lower level than a
    token that was present and *wrong*.
    """
    raw = ""
    getter = getattr(headers, "get", None)
    if getter is not None:
        raw = str(getter("Authorization") or getter("authorization") or "")
    if not raw:
        raise AuthError("missing_token", "no Authorization header")
    scheme, _, token = raw.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AuthError("missing_token", "Authorization was not a bearer token")
    return token.strip()


def verify_notification(
    config: Config,
    headers: Any,
    *,
    keys: KeySource | None = None,
    now: float | None = None,
    issuers: tuple[str, ...] = ACCEPTED_ISSUERS,
) -> dict[str, Any]:
    """Prove an inbound webhook request came from Microsoft; return its claims.

    Checked, in this order: the header names a real asymmetric algorithm (never
    ``none``, never an HMAC — an attacker choosing the algorithm is the classic
    JWT forgery); the issuer is one this bot accepts; the audience is *this*
    bot's app id, so a token minted for someone else's bot is refused; the
    token is inside its validity window; and finally the signature checks out
    against the key the ``kid`` names.

    Raises:
        AuthError: naming the specific failure. There is no path through this
            function that returns without a verified signature — in particular
            a missing :class:`KeySource` and a missing ``cryptography`` are
            refusals (``no_key_source``, ``crypto_unavailable``), not
            exemptions.
    """
    token = bearer_token(headers)
    parts = token.split(".")
    if len(parts) != 3:
        raise AuthError("malformed_token", "a JWS has three dot-separated parts")
    header = _decode_segment(parts[0], "JOSE header")
    claims = _decode_segment(parts[1], "claim set")

    alg = str(header.get("alg", ""))
    if alg not in ("RS256", "RS384", "RS512"):
        # "none" and the HS* family are the two ways a caller talks a verifier
        # into checking a signature it can forge.
        raise AuthError("bad_algorithm", f"refusing alg {alg!r}")

    issuer = str(claims.get("iss", ""))
    if issuer not in issuers:
        raise AuthError("bad_issuer", f"refusing issuer {issuer!r}")

    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    if config.app_id not in [str(a) for a in audiences if a is not None]:
        raise AuthError("bad_audience", "the token was not minted for this app id")

    stamp = time.time() if now is None else float(now)
    expiry = _numeric(claims, "exp")
    if expiry is None:
        raise AuthError("expired", "the token carries no exp")
    if stamp > expiry + CLOCK_SKEW_S:
        raise AuthError("expired", "the token expired")
    not_before = _numeric(claims, "nbf")
    if not_before is not None and stamp + CLOCK_SKEW_S < not_before:
        raise AuthError("not_yet_valid", "the token is not valid yet")
    issued = _numeric(claims, "iat")
    if issued is not None and stamp - issued > MAX_TOKEN_AGE_S + CLOCK_SKEW_S:
        raise AuthError("stale", "the token was issued too long ago")

    if keys is None:
        raise AuthError(
            "no_key_source",
            "no signing keys are configured, so no token can be verified; "
            "pass keys=OpenIdKeys() or keys=StaticKeys({...}) to make_server",
        )
    key = keys.public_key(str(header.get("kid", "")))
    signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
    _verify_signature(alg, key, signing_input, _b64url(parts[2]))
    return claims


def _numeric(claims: dict[str, Any], name: str) -> float | None:
    value = claims.get(name)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise AuthError("malformed_token", f"{name} was not a number") from exc


def _verify_signature(
    alg: str, key: Any, signing_input: bytes, signature: bytes
) -> None:
    """RS256/384/512 over ``signing_input``. Raises :class:`AuthError`.

    ``cryptography`` is imported here rather than at module import so that this
    package stays importable — for the service's integrations report, for
    ``--help``, for the config check — on a machine that does not have it. Its
    absence is then a named refusal at the one moment it matters.
    """
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise AuthError(
            "crypto_unavailable",
            "RS256 verification needs the 'cryptography' package; without it "
            "this endpoint refuses every request rather than accepting one it "
            "cannot check",
        ) from exc

    if not isinstance(key, rsa.RSAPublicKey):
        raise AuthError("bad_key", "the signing key is not an RSA public key")
    digest = {"RS256": hashes.SHA256, "RS384": hashes.SHA384, "RS512": hashes.SHA512}
    try:
        key.verify(signature, signing_input, padding.PKCS1v15(), digest[alg]())
    except InvalidSignature as exc:
        raise AuthError("bad_signature", "the signature did not check out") from exc


class StaticKeys:
    """Signing keys supplied up front, by ``kid``.

    For an operator who pins Microsoft's keys out of band, and for tests, which
    generate their own key pair and never touch the network.
    """

    def __init__(self, keys: dict[str, Any]):
        self._keys = dict(keys)

    def public_key(self, kid: str) -> Any:
        try:
            return self._keys[kid]
        except KeyError as exc:
            raise AuthError("unknown_kid", f"no signing key for kid {kid!r}") from exc


class OpenIdKeys:
    """Microsoft's published signing keys, fetched and cached.

    The two OpenID metadata documents in :data:`OPENID_CONFIGURATION_URLS` name
    a JWKS each; both are read, because the calling platform and the Bot
    Framework channel sign with different keys. Every URL — including the
    ``jwks_uri`` the *document* names — is checked against :data:`KEY_HOSTS`
    before it is fetched, so a compromised or spoofed metadata document cannot
    redirect key discovery at a host of its choosing.

    ``fetch`` is the seam: production uses urllib with redirects refused, tests
    hand in a recorded dict.
    """

    def __init__(
        self,
        *,
        fetch: Any = None,
        ttl_s: float = 3600.0,
        configuration_urls: tuple[str, ...] = OPENID_CONFIGURATION_URLS,
        now: Any = time.monotonic,
    ):
        self._fetch = fetch or _urllib_fetch
        self._ttl_s = ttl_s
        self._urls = configuration_urls
        self._now = now
        self._lock = threading.Lock()
        self._keys: dict[str, Any] = {}
        self._loaded_at = 0.0

    def public_key(self, kid: str) -> Any:
        with self._lock:
            fresh = self._keys and self._now() - self._loaded_at < self._ttl_s
            if not fresh:
                self._refresh()
            try:
                return self._keys[kid]
            except KeyError:
                # A rotated key is the normal reason a kid is unknown, so one
                # forced refresh is worth a network call. A second miss is a
                # refusal, not a retry loop.
                self._refresh()
            try:
                return self._keys[kid]
            except KeyError as exc:
                raise AuthError(
                    "unknown_kid", f"no published signing key for kid {kid!r}"
                ) from exc

    def _refresh(self) -> None:
        keys: dict[str, Any] = {}
        for url in self._urls:
            document = self._get_json(url)
            jwks_uri = str(document.get("jwks_uri", ""))
            if not jwks_uri:
                raise AuthError("key_fetch_failed", f"{url} named no jwks_uri")
            keys.update(_keys_from_jwks(self._get_json(jwks_uri)))
        if not keys:
            raise AuthError("key_fetch_failed", "no usable signing keys were published")
        self._keys = keys
        self._loaded_at = self._now()

    def _get_json(self, url: str) -> dict[str, Any]:
        ensure_key_url(url)
        try:
            body = self._fetch(url)
        except AuthError:
            raise
        except Exception as exc:  # a transport failure, whatever its type
            raise AuthError("key_fetch_failed", f"{url}: {exc}") from exc
        try:
            text = body.decode("utf-8") if isinstance(body, bytes) else body
            payload = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AuthError("key_fetch_failed", f"{url} was not JSON") from exc
        if not isinstance(payload, dict):
            raise AuthError("key_fetch_failed", f"{url} was not a JSON object")
        return payload


def ensure_key_url(url: str) -> str:
    """Return ``url`` unchanged if it is https to an allowlisted key host.

    Enforced at request-construction time, so it holds for every fetch seam
    including the fakes: code that builds a key request for another host is
    wrong whether or not that request would have left the machine.
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        raise AuthError(
            "bad_host", f"refusing non-https key URL scheme {parsed.scheme!r}"
        )
    if parsed.hostname not in KEY_HOSTS:
        raise AuthError(
            "bad_host", f"refusing to fetch signing keys from {parsed.hostname!r}"
        )
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: a 3xx is how a fetch leaves the allowlist."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _urllib_fetch(url: str, timeout_s: float = 15.0) -> bytes:  # pragma: no cover
    opener = urllib.request.build_opener(_NoRedirect())
    request = urllib.request.Request(ensure_key_url(url), method="GET")
    try:
        with opener.open(request, timeout=timeout_s) as response:
            return response.read(1 << 20)
    except urllib.error.HTTPError as exc:
        raise AuthError("key_fetch_failed", f"{url}: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise AuthError("key_fetch_failed", f"{url}: {exc.reason}") from exc


def _keys_from_jwks(document: dict[str, Any]) -> dict[str, Any]:
    """RSA public keys out of a JWKS, by ``kid``. Non-RSA entries are skipped."""
    try:
        from cryptography.hazmat.primitives.asymmetric import rsa
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise AuthError(
            "crypto_unavailable", "reading a JWKS needs the 'cryptography' package"
        ) from exc

    keys: dict[str, Any] = {}
    for entry in document.get("keys") or []:
        if not isinstance(entry, dict) or entry.get("kty") != "RSA":
            continue
        kid = str(entry.get("kid", ""))
        try:
            modulus = int.from_bytes(_b64url(str(entry["n"])), "big")
            exponent = int.from_bytes(_b64url(str(entry["e"])), "big")
        except (KeyError, AuthError):
            continue
        keys[kid] = rsa.RSAPublicNumbers(exponent, modulus).public_key()
    return keys


# --------------------------------------------------------------------------
# What arrived
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Incoming:
    """One authenticated thing to act on.

    ``kind`` is ``"call"`` (a Graph call lifecycle notification) or
    ``"message"`` (a Bot Framework activity). They are one type because the
    dispatcher's gates — dedupe, allowlist, run memory, slot — are the same for
    both, and the runner is the only thing that cares about the difference.
    """

    kind: str
    notification_id: str
    meeting_id: str
    conversation_id: str
    text: str = ""
    sender: str = ""
    change_type: str = ""
    resource: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def follow_up(self) -> str:
        """``review`` / ``order`` / ``status`` when the text is a follow-up."""
        first = self.text.strip().split(" ")[0].strip(":,").lower()
        return first if first in FOLLOW_UPS else ""


def _derived_id(payload: Any) -> str:
    """A deterministic id for a notification that carries none.

    Falling back to a random id, or to no dedupe at all, would make every
    retry a fresh *paid* run — the exact failure the id memory exists to
    prevent. Hashing the notification's own content means a retry of the same
    notification derives the same id.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def incoming_from_calls(payload: dict[str, Any]) -> list[Incoming]:
    """Parse a Graph ``communications/calls`` notification body.

    The shape is ``{"value": [{changeType, resource, resourceUrl,
    resourceData}, ...]}``. An entry that is not an object is skipped rather
    than raising: one malformed entry must not discard the notification's other
    entries, and there is nothing to act on in it either way.
    """
    entries = payload.get("value")
    if not isinstance(entries, list):
        return []
    out: list[Incoming] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        data = entry.get("resourceData")
        data = data if isinstance(data, dict) else {}
        chat_info = data.get("chatInfo")
        chat_info = chat_info if isinstance(chat_info, dict) else {}
        meeting_info = data.get("meetingInfo")
        meeting_info = meeting_info if isinstance(meeting_info, dict) else {}
        resource = str(entry.get("resource", ""))
        meeting_id = str(
            chat_info.get("threadId")
            or meeting_info.get("organizerId")
            or _call_id(resource)
            or ""
        )
        out.append(
            Incoming(
                kind="call",
                notification_id=str(entry.get("id") or "") or _derived_id(entry),
                meeting_id=meeting_id,
                conversation_id=str(chat_info.get("threadId") or meeting_id),
                change_type=str(entry.get("changeType", "")),
                resource=resource,
                raw=entry,
            )
        )
    return out


def _call_id(resource: str) -> str:
    """``/communications/calls/<id>`` -> ``<id>``."""
    parts = [part for part in str(resource).split("/") if part]
    return parts[-1] if parts else ""


def incoming_from_activity(payload: dict[str, Any]) -> Incoming | None:
    """Parse one Bot Framework activity, or ``None`` when it is not for us.

    Only ``message`` activities carry a request. The bot's own messages are
    dropped by id, because two bots in one meeting chat would otherwise answer
    each other indefinitely.
    """
    if str(payload.get("type", "")) != "message":
        return None
    conversation = payload.get("conversation")
    conversation = conversation if isinstance(conversation, dict) else {}
    sender = payload.get("from")
    sender = sender if isinstance(sender, dict) else {}
    recipient = payload.get("recipient")
    recipient = recipient if isinstance(recipient, dict) else {}
    sender_id = str(sender.get("id", ""))
    if sender_id and sender_id == str(recipient.get("id", "")):
        return None
    channel_data = payload.get("channelData")
    channel_data = channel_data if isinstance(channel_data, dict) else {}
    meeting = channel_data.get("meeting")
    meeting = meeting if isinstance(meeting, dict) else {}
    conversation_id = str(conversation.get("id", ""))
    return Incoming(
        kind="message",
        notification_id=str(payload.get("id") or "") or _derived_id(payload),
        meeting_id=str(meeting.get("id") or conversation_id),
        conversation_id=conversation_id,
        text=_strip_mentions(str(payload.get("text", ""))),
        sender=sender_id,
        raw=payload,
    )


#: ``<at>Kaleo</at>`` — the mention markup Teams puts in an activity's text,
#: display name and all. The name goes with the tag: leaving "Kaleo" in front
#: of the request would put the bot's own name into the prompt that becomes a
#: circuit.
_MENTION = re.compile(r"<at\b[^>]*>.*?</at>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]*>")


def _strip_mentions(text: str) -> str:
    """The request as a person typed it: mentions and markup removed."""
    return " ".join(_TAG.sub(" ", _MENTION.sub(" ", text)).split())


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------


class Runner(Protocol):
    """What the dispatcher hands accepted work to.

    One method, because the dispatcher's job ends the moment a request is
    proved authentic, new, in scope and given a slot. ``teamsbot.runner`` is
    the module that knows both Teams and silkscreen; this one knows only HTTP.

    Whatever ``handle`` returns — a :class:`~teamsbot.runner.TeamsReport`, or
    nothing — is what the meeting's run memory remembers, so a follow-up can
    refer to it.
    """

    def handle(self, incoming: Incoming) -> Any: ...


@dataclass
class RunRecord:
    """What one meeting's last run produced, for a follow-up to refer to."""

    meeting_id: str
    request: str
    summary: str = ""
    at: float = 0.0


class RunMemory:
    """Per-meeting memory of the last run. **In-process only.**

    Deliberately not persisted. A restart therefore loses it, and the honest
    answer to a follow-up after a restart is :data:`MISSING_RUN_NOTE` — saying
    so, rather than acting on whatever board happens to be lying around, which
    is the failure that would actually cost someone money.
    """

    def __init__(self, limit: int = 200):
        self._limit = limit
        self._lock = threading.Lock()
        self._records: OrderedDict[str, RunRecord] = OrderedDict()

    def remember(self, record: RunRecord) -> None:
        with self._lock:
            self._records[record.meeting_id] = record
            self._records.move_to_end(record.meeting_id)
            while len(self._records) > self._limit:
                self._records.popitem(last=False)

    def recall(self, meeting_id: str) -> RunRecord | None:
        with self._lock:
            return self._records.get(meeting_id)


def _summarise(outcome: Any) -> str:
    """One line about what a run produced, for the run memory.

    Deliberately shallow: the dispatcher does not know a ``TeamsReport`` from a
    ``None`` and must not start guessing at one. What matters for a follow-up
    is that *something* ran and roughly what it said.
    """
    if outcome is None:
        return ""
    built = getattr(outcome, "built", None)
    if isinstance(built, list):
        via = getattr(outcome, "spoke_via", "?")
        return f"{len(built)} board(s) built, spoke via {via}"
    return str(outcome)[:400]


class _SeenNotifications:
    """Bounded set of notification ids already handled.

    Teams retries a notification it does not see acknowledged quickly, and a
    retry that slips through is a second *paid* pipeline run for one request. A
    slow first response and a genuine retry look identical from here, so ids
    are remembered rather than inferred.
    """

    def __init__(self, limit: int = 2048):
        self._limit = limit
        self._lock = threading.Lock()
        self._ids: OrderedDict[str, None] = OrderedDict()

    def add_if_new(self, notification_id: str) -> bool:
        if not notification_id:
            # Never reachable through the parsers, which derive an id from the
            # content; belt and braces for a caller that builds an Incoming by
            # hand. An id-less notification is dropped, not run: an unbounded
            # "always new" would be the expensive direction to be wrong in.
            return False
        with self._lock:
            if notification_id in self._ids:
                return False
            self._ids[notification_id] = None
            while len(self._ids) > self._limit:
                self._ids.popitem(last=False)
            return True


class Dispatcher:
    """Decides what an authenticated Teams payload means, and runs it off-thread."""

    def __init__(
        self,
        config: Config,
        runner: Runner,
        *,
        speaker: Any = None,
        memory: RunMemory | None = None,
        slot_wait_s: float = SLOT_WAIT_S,
        max_concurrent: int = MAX_CONCURRENT_RUNS,
    ):
        self.config = config
        self.runner = runner
        self.speaker = speaker
        self.memory = memory or RunMemory()
        self.slot_wait_s = slot_wait_s
        self._slots = threading.BoundedSemaphore(max(1, max_concurrent))
        self._seen = _SeenNotifications()
        self._threads: list[threading.Thread] = []

    # -- routes -----------------------------------------------------------

    def handle_call_payload(self, payload: dict[str, Any]) -> tuple[int, Any]:
        """Map one Graph calling notification body to a response."""
        accepted = 0
        for incoming in incoming_from_calls(payload):
            if self._accept(incoming):
                accepted += 1
        # 202 is what a calling bot answers: the notification was taken, the
        # work has not happened yet, and Teams should not retry it.
        return 202, {"ok": True, "accepted": accepted}

    def handle_activity_payload(self, payload: dict[str, Any]) -> tuple[int, Any]:
        """Map one Bot Framework activity to a response."""
        incoming = incoming_from_activity(payload)
        if incoming is None:
            return 200, {"ok": True, "ignored": "not a message activity"}
        accepted = self._accept(incoming)
        return 202 if accepted else 200, {"ok": True, "accepted": int(accepted)}

    # -- gates ------------------------------------------------------------

    def _accept(self, incoming: Incoming) -> bool:
        if not self._seen.add_if_new(incoming.notification_id):
            log.info("ignoring duplicate delivery of %s", incoming.notification_id)
            return False
        if not self.config.allows(incoming.meeting_id):
            log.info(
                "ignoring a meeting outside the allowlist: %s", incoming.meeting_id
            )
            return False
        follow_up = incoming.follow_up
        if follow_up and self.memory.recall(incoming.meeting_id) is None:
            # The run this refers to is gone. Say so; do not start a new one
            # and do not answer about a different board.
            self._spawn(lambda: self._say(incoming, MISSING_RUN_NOTE))
            return False
        self._spawn(lambda: self._run_with_slot(incoming))
        return True

    def _run_with_slot(self, incoming: Incoming) -> None:
        if not self._slots.acquire(timeout=self.slot_wait_s):
            self._say(
                incoming, "I'm still finishing the last board — try again shortly."
            )
            return
        try:
            outcome = self.runner.handle(incoming)
            # Remembered only on the way out, and only for a run that did not
            # raise: a follow-up must not be answered from a run that failed
            # halfway. The record is the meeting's, not the notification's,
            # because "review" arrives as a different notification entirely.
            self.memory.remember(
                RunRecord(
                    meeting_id=incoming.meeting_id,
                    request=incoming.text,
                    summary=_summarise(outcome),
                    at=time.time(),
                )
            )
        except Exception:  # noqa: BLE001 - a worker thread must not die silently
            log.exception("run failed for meeting %s", incoming.meeting_id)
            self._say(
                incoming,
                "That run failed before it produced a board. The error is in the "
                "bot's log.",
            )
        finally:
            self._slots.release()

    def _say(self, incoming: Incoming, text: str) -> None:
        """Speak a note back, naming the failure if the speaker cannot.

        A speaker that raises is reported into the log rather than swallowed:
        "the bot said nothing and nobody knows why" is precisely the state the
        ``Speaker`` seam exists to make impossible.
        """
        speaker = self.speaker
        if speaker is None:
            log.warning("no speaker configured; not said: %s", text)
            return
        try:
            speaker.say(incoming.conversation_id or incoming.meeting_id, text)
        except Exception as exc:  # noqa: BLE001 - name it, never hide it
            log.error(
                "speaker %r could not say it: %s",
                getattr(speaker, "name", "?"),
                exc,
            )

    # -- threads ----------------------------------------------------------

    def _spawn(self, work: Any) -> threading.Thread:
        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        self._threads.append(thread)
        self._threads = [t for t in self._threads if t.is_alive()]
        return thread

    def join(self, timeout: float = 30.0) -> None:
        """Wait for scheduled work. For tests and shutdown, not the hot path."""
        for thread in list(self._threads):
            thread.join(timeout)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def make_handler(
    dispatcher: Dispatcher, *, keys: KeySource | None = None, now: Any = None
) -> type[BaseHTTPRequestHandler]:
    """Build a handler class bound to one dispatcher and one key source."""

    config = dispatcher.config

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "silkscreen-teams"
        #: Applied to the socket by ``StreamRequestHandler.setup``. Without it
        #: an unauthenticated client that dribbles a body one byte at a time
        #: holds a handler thread indefinitely, and this server is
        #: thread-per-connection.
        timeout = SOCKET_TIMEOUT_S

        # -- plumbing ----------------------------------------------------

        def _send(self, code: int, payload: Any, content_type: str = "") -> None:
            if isinstance(payload, (dict, list)):
                body = json.dumps(payload).encode("utf-8")
                content_type = content_type or "application/json; charset=utf-8"
            else:
                body = str(payload).encode("utf-8")
                content_type = content_type or "text/plain; charset=utf-8"
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self) -> bytes | None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._send(400, "bad Content-Length")
                return None
            if length > MAX_BODY_BYTES:
                # Drain a little before answering, so a well-behaved client
                # gets the 413 explaining itself rather than a reset socket —
                # but bounded, in bytes and in wall-clock time, because this
                # happens before authentication.
                self.close_connection = True
                if self._drain(min(length, MAX_DRAIN_BYTES)):
                    self._send(413, "body too large")
                return None
            return self.rfile.read(length) if length > 0 else b""

        def _drain(self, length: int, chunk: int = 64 << 10) -> bool:
            deadline = time.monotonic() + DRAIN_DEADLINE_S
            connection = getattr(self, "connection", None)
            read1 = getattr(self.rfile, "read1", self.rfile.read)
            remaining = length
            try:
                while remaining > 0:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        return False
                    if connection is not None:
                        connection.settimeout(min(SOCKET_TIMEOUT_S, left))
                    block = read1(min(chunk, remaining))
                    if not block:
                        return True
                    remaining -= len(block)
                return True
            except OSError:  # timeout, or a client that gave up mid-dribble
                return False
            finally:
                if connection is not None:
                    with contextlib.suppress(OSError):
                        connection.settimeout(SOCKET_TIMEOUT_S)

        def _authenticated_body(
            self, issuers: tuple[str, ...] = ACCEPTED_ISSUERS
        ) -> bytes | None:
            """Read the body and prove the request came from Microsoft, or 401.

            The order is the point: the token is verified before the body is
            handed to a parser. The client is told "unauthorized" and nothing
            else — which check failed is in the log, not in the response.

            ``issuers`` is per route, and the default is the *narrow* one: a
            new route added without thinking about it gets the Bot Framework
            list, not the wider calling list. The failure of forgetting is then
            a refusal to fix rather than an acceptance to discover.
            """
            body = self._read_body()
            if body is None:
                return None
            try:
                verify_notification(
                    config,
                    self.headers,
                    keys=keys,
                    now=None if now is None else now(),
                    issuers=issuers,
                )
            except AuthError as exc:
                log.warning("refused %s: %s", self.path, exc)
                self._send(401, "unauthorized")
                return None
            return body

        def _json_body(
            self, issuers: tuple[str, ...] = ACCEPTED_ISSUERS
        ) -> dict[str, Any] | None:
            body = self._authenticated_body(issuers)
            if body is None:
                return None
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send(400, "body was not JSON")
                return None
            if not isinstance(payload, dict):
                self._send(400, "expected a JSON object")
                return None
            return payload

        # -- routes ------------------------------------------------------

        def do_GET(self) -> None:
            if self.path.split("?")[0] == "/healthz":
                self._send(200, {"ok": True, "service": "silkscreen-teams"})
                return
            self._send(404, "not found")

        def do_POST(self) -> None:
            route = self.path.split("?")[0]
            if route == "/api/calls":
                # Graph signs calling notifications as itself; see
                # CALLING_ISSUERS.
                payload = self._json_body(CALLING_ISSUERS)
                if payload is None:
                    return
                code, response = dispatcher.handle_call_payload(payload)
                self._send(code, response)
            elif route == "/api/messages":
                payload = self._json_body()
                if payload is None:
                    return
                code, response = dispatcher.handle_activity_payload(payload)
                self._send(code, response)
            else:
                self._send(404, "not found")

        def log_message(self, fmt: str, *args: Any) -> None:
            log.info("%s - %s", self.address_string(), fmt % args)

    return Handler


def make_server(
    config: Config,
    runner: Runner | None = None,
    *,
    speaker: Any = None,
    keys: KeySource | None = None,
    port: int | None = None,
) -> ThreadingHTTPServer:
    """Build the server. ``runner`` and ``speaker`` are the injection seams.

    The default key source is :class:`OpenIdKeys`, which fetches Microsoft's
    published keys on the first request. It is constructed rather than fetched
    here, so a startup with no network still starts and fails per-request with
    a named reason.

    ``port=None`` takes the port from ``config`` (``TEAMS_PORT``, default
    3978 -- the Bot Framework convention every sample and ngrok recipe uses).
    An explicit ``port`` wins, which is how a test binds port 0.
    """
    if speaker is None:
        from .speak import speaker_for

        speaker = speaker_for(config)
    if runner is None:
        runner = _LazyRunner(config, speaker)
    dispatcher = Dispatcher(config, runner, speaker=speaker)
    handler = make_handler(dispatcher, keys=keys or OpenIdKeys())
    listen = port if port is not None else getattr(config, "port", DEFAULT_PORT)
    return ThreadingHTTPServer(("0.0.0.0", int(listen)), handler)


# A typed chat message used to be wrapped into a transcript chunk here, on the
# way to ``runner.run_meeting``. It is wrapped in ``runner.handle_incoming``
# instead (``runner._Utterance``), so the chunk shape lives beside the gates
# that read it rather than in the module that used to skip them.


class _LazyRunner:
    """The default runner: ``teamsbot.runner``, imported when work arrives.

    Deferred so the HTTP surface, ``--help`` and the config check stay usable
    when the engine or a sibling module is not installed, and so a broken
    import is reported against the request that needed it rather than at import
    time. Nothing here is silent: a missing runner, a missing model and a
    notification with nothing in it to act on are different outcomes with
    different sentences.

    **This calls :func:`teamsbot.runner.handle_incoming` and nothing else.**
    It used to call ``runner.run_meeting`` directly, which quietly stepped past
    every judgement ``handle_incoming`` exists to make: the ``order`` refusal
    (:data:`~teamsbot.runner.NO_ORDER_NOTE`) was unreachable in production, so
    typing "order the boards" into a meeting bought a paid Gemini call and an
    intent extraction instead of one free sentence declining; a ``review`` or
    ``status`` follow-up did the same rather than answering
    :data:`~teamsbot.runner.FOLLOW_UP_NOTE`; a ``call`` notification was
    dropped with a log line instead of the report that says a transcript read
    needs a Graph client; and the summary was addressed to the meeting id
    rather than the conversation id, which ``ChatSpeaker`` cannot deliver to.
    Every gate that matters lives one import away, so the production path must
    go through it -- the *only* judgement made here is that a chat message with
    no text is not a request, which keeps a paid call off an empty utterance.
    """

    def __init__(self, config: Config, speaker: Any):
        self.config = config
        self.speaker = speaker

    def handle(self, incoming: Incoming) -> Any:
        if incoming.kind == "message" and not incoming.text.strip():
            log.info(
                "nothing to run for a %s notification (%s) in %s: no text",
                incoming.kind,
                incoming.change_type or "no changeType",
                incoming.meeting_id,
            )
            return None
        try:
            from .runner import handle_incoming
        except ImportError as exc:
            raise RuntimeError(
                f"cannot run: {exc}. The Teams front end needs teamsbot.runner "
                f"and the silkscreen engine installed in the same environment."
            ) from exc
        # ``model`` is deliberately not passed: ``handle_incoming`` builds one
        # only once it has decided there is something to run, so a refused
        # follow-up needs neither an API key nor a model call.
        return handle_incoming(self.config, incoming, speaker=self.speaker)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - entry point
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    server = make_server(config)
    for key, value in config.redacted().items():
        log.info("config %s = %s", key, value)
    log.info(
        "listening on :%s (POST /api/calls, POST /api/messages)", server.server_port
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

"""One HTTP seam for every Google call, with a hard host allowlist.

Stdlib only, for the same reason ``service/app.py`` is: the handful of REST
calls this package makes do not justify a client library. Every request goes
through :class:`Transport`, an injectable callable, so the tests exercise the
real request construction -- URL, method, headers, body encoding -- against a
recorded transport instead of the network.

The allowlist is enforced at request-construction time, not inside the real
transport, so it holds for *every* transport including the fakes: code that
builds a request for a non-Google host is wrong whether or not that request
would have left the machine.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

__all__ = [
    "ALLOWED_HOSTS",
    "GoogleError",
    "HttpRequest",
    "HttpResponse",
    "Transport",
    "describe_secret",
    "ensure_google_url",
    "error_detail",
    "mask",
    "urllib_transport",
]

#: The only hosts this package will ever address. Exact matches -- a suffix
#: check would wave through ``chat.googleapis.com.evil.example``.
#:
#: **CONFIRMED 2026-09-08** against Google's own discovery documents, which are
#: the machine-readable source for each service's base URL: Gmail v1 reports
#: ``rootUrl`` ``https://gmail.googleapis.com/`` and Calendar v3 reports
#: ``https://www.googleapis.com/`` with ``servicePath`` ``calendar/v3/``, so
#: both spellings below are current rather than legacy. ``oauth2.googleapis.com``
#: is the token endpoint (``accounts.google.com`` is deliberately absent -- it
#: is opened in the user's browser, never addressed by this client), and
#: ``chat.googleapis.com`` is the incoming-webhook host.
#:
#: **The redirect refusal below is safe here**: none of the four endpoints this
#: package calls -- the token POST, ``messages/send``, ``events`` insert, and a
#: Chat webhook POST -- is documented to answer 3xx, and each is addressed at
#: its canonical host with no alias in the path. If one ever does redirect the
#: transport reports it as an ordinary failed response with the 3xx status,
#: which surfaces as ``GoogleError("http_302", ...)``: loud, named, and with
#: no bearer token following the ``Location``.
ALLOWED_HOSTS = frozenset(
    {
        "oauth2.googleapis.com",
        "gmail.googleapis.com",
        "www.googleapis.com",
        "chat.googleapis.com",
    }
)


class GoogleError(RuntimeError):
    """A Google call failed, or was refused before it was made.

    ``code`` is Google's own error status when one came back (``401``,
    ``invalid_grant``), or a local reason (``bad_host``) when the request was
    never sent.
    """

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def mask(secret: str, keep: int = 4) -> str:
    """A printable stand-in for a secret: its tail, never its value.

    Used for every log or terminal line that has to prove a setting is
    present. Short values show nothing at all rather than most of themselves.
    """
    secret = secret or ""
    if len(secret) <= keep * 2:
        return "<set>" if secret else "<unset>"
    return f"…{secret[-keep:]}"


def describe_secret(secret: str) -> str:
    """For a secret whose *tail* is the sensitive part -- a webhook URL ends
    in its token -- show only that it is set and how long it is."""
    return f"<set, {len(secret)} chars>" if secret else "<unset>"


def ensure_google_url(url: str) -> str:
    """Return ``url`` unchanged if it is https to an allowlisted Google host.

    Raises:
        GoogleError: for any other scheme or host. The refusal happens before
            a transport sees the request, so no byte -- and no bearer token --
            can leave toward a host this package does not know.
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        raise GoogleError(
            "bad_host", f"refusing non-https URL scheme {parsed.scheme!r}"
        )
    if parsed.hostname not in ALLOWED_HOSTS:
        raise GoogleError(
            "bad_host",
            f"refusing to send to {parsed.hostname!r}: not a known Google API host",
        )
    return url


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes

    def json(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GoogleError(
                "bad_response", f"HTTP {self.status} was not JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise GoogleError("bad_response", "expected a JSON object")
        return payload


class Transport(Protocol):
    def __call__(self, request: HttpRequest) -> HttpResponse: ...


def error_detail(response: HttpResponse, fallback: str) -> str:
    """Google's own error text out of a failed response, when there is one.

    The REST APIs put it at ``error.message`` and the token endpoint at
    ``error_description``; a body that is not JSON -- a proxy's HTML page,
    an empty 403 -- yields ``fallback``. Google never echoes a bearer token
    or a webhook secret back in that text, so it is safe to show.
    """
    try:
        payload = response.json()
    except GoogleError:
        return fallback
    error = payload.get("error")
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])
    if payload.get("error_description"):
        return str(payload["error_description"])
    return fallback


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect.

    ``urllib`` would otherwise follow a 3xx and re-send the request -- with
    its ``Authorization`` header -- to whatever host the redirect names,
    which is exactly the path the allowlist exists to close. Returning
    ``None`` makes ``urlopen`` raise the 3xx as an ``HTTPError``, which the
    transport then hands back as an ordinary failed response.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def urllib_transport(timeout: float = 30.0) -> Transport:
    """The real transport. An HTTP error status is a response, not an
    exception -- Google puts the useful error JSON in the body of a 4xx."""

    opener = urllib.request.build_opener(_NoRedirect())

    def send(request: HttpRequest) -> HttpResponse:
        ensure_google_url(request.url)
        req = urllib.request.Request(
            request.url,
            data=request.body or None,
            headers=request.headers,
            method=request.method,
        )
        try:
            with opener.open(req, timeout=timeout) as resp:
                return HttpResponse(resp.status, resp.read())
        except urllib.error.HTTPError as exc:
            return HttpResponse(exc.code, exc.read())
        except urllib.error.URLError as exc:
            raise GoogleError("network_error", str(exc.reason)) from exc

    return send

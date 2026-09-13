"""One HTTP seam for every Stripe call, with a hard host allowlist.

Stdlib only, the same decision ``googleapps/transport.py`` and
``service/app.py`` already made: this package makes a handful of REST calls
and does not justify pulling in the Stripe SDK. Every request goes through
:class:`Transport`, an injectable callable, so tests exercise the real request
construction -- URL, method, headers, form encoding, idempotency key -- against
a recorded transport instead of the network.

The allowlist is enforced at request-construction time rather than inside the
real transport, so it holds for *every* transport including the fakes.

Two Stripe-specific details this module owns:

* Stripe's API is **form-encoded**, not JSON, and nests with bracket notation
  (``line_items[0][price]``). ``form_encode`` is the only place that is known.
* Redirects are refused. A 3xx would otherwise carry the Authorization header,
  and therefore the API key, to wherever the redirect pointed.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

from .errors import StripeError

__all__ = [
    "ALLOWED_HOSTS",
    "OAUTH_HOSTS",
    "HttpRequest",
    "HttpResponse",
    "Transport",
    "ensure_stripe_url",
    "form_encode",
    "mask_key",
    "urllib_transport",
]

#: The only host a *REST* call will ever address. An exact match: a suffix
#: check would wave through ``api.stripe.com.evil.example``.
ALLOWED_HOSTS = frozenset({"api.stripe.com"})

#: The hosts the OAuth flow in :mod:`billing.oauth` addresses, and *only* it.
#: Kept a separate set rather than merged into :data:`ALLOWED_HOSTS` so that
#: the guarantee "an API key can never travel anywhere but api.stripe.com"
#: survives this feature: a request has to opt in per-call to reach these,
#: and the one place that does never attaches a secret key.
OAUTH_HOSTS = frozenset({"access.stripe.com", "mcp.stripe.com"})


@dataclass(frozen=True)
class HttpRequest:
    url: str
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | None = None
    #: Which Stripe hosts *this* request may address. Defaults to the REST
    #: host, so widening it is always an explicit, visible act at the call
    #: site rather than a global relaxation.
    hosts: frozenset[str] = ALLOWED_HOSTS

    def __post_init__(self) -> None:
        # Enforced HERE, not inside the real transport, so the guarantee
        # holds for every transport including the test fakes. The docstring
        # claimed this; until now only ``urllib_transport`` checked.
        ensure_stripe_url(self.url, hosts=self.hosts)

    def __repr__(self) -> str:
        """Redacted. This object is in the frame of every transport failure,
        and its headers carry ``Authorization: Bearer <key>``."""
        safe = {
            k: (
                mask_key(v.removeprefix("Bearer "))
                if k.lower() == "authorization"
                else v
            )
            for k, v in self.headers.items()
        }
        size = len(self.body) if self.body else 0
        return (
            f"HttpRequest({self.method} {self.url!r}, "
            f"headers={safe!r}, body={size}b)"
        )


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StripeError(
                f"Stripe returned a body that is not JSON: {exc}"
            ) from None


class Transport(Protocol):
    def __call__(self, request: HttpRequest) -> HttpResponse: ...


def mask_key(key: str) -> str:
    """``rk_test_51abcdef...`` -> ``rk_test_…cdef``.

    Never returns enough to use and always keeps the prefix, because the
    prefix is the part worth seeing in a log: it says test vs live and
    restricted vs secret.
    """
    if not key:
        return "<unset>"
    head, _, rest = key.partition("_")
    kind, _, body = rest.partition("_")
    prefix = f"{head}_{kind}_" if body else f"{head}_"
    tail = (body or rest)[-4:] if len(body or rest) >= 4 else ""
    return f"{prefix}…{tail}"


def ensure_stripe_url(url: str, *, hosts: frozenset[str] = ALLOWED_HOSTS) -> str:
    """Return ``url`` if it addresses Stripe over https, else raise."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        raise StripeError(
            f"refusing a non-https Stripe URL: {parsed.scheme or '<none>'}"
        )
    if parsed.hostname not in hosts:
        raise StripeError(f"refusing to address a non-Stripe host: {parsed.hostname!r}")
    return url


def form_encode(params: dict[str, Any], _prefix: str = "") -> bytes:
    """Encode ``params`` the way Stripe's API expects.

    Nested dicts and lists use bracket notation, booleans lower-case, and
    ``None`` is dropped entirely rather than sent as the string "None" --
    which Stripe would accept and store.
    """
    pairs: list[tuple[str, str]] = []

    def walk(key: str, value: Any) -> None:
        if value is None:
            return
        if isinstance(value, bool):
            pairs.append((key, "true" if value else "false"))
        elif isinstance(value, dict):
            for k, v in value.items():
                walk(f"{key}[{k}]" if key else str(k), v)
        elif isinstance(value, (list, tuple)):
            for i, v in enumerate(value):
                walk(f"{key}[{i}]", v)
        else:
            pairs.append((key, str(value)))

    walk(_prefix, params)
    return urllib.parse.urlencode(pairs).encode("utf-8")


def urllib_transport(request: HttpRequest, *, timeout: float = 30.0) -> HttpResponse:
    """The real transport. Refuses redirects so the API key cannot travel."""

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args: Any, **kwargs: Any) -> None:
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(
        ensure_stripe_url(request.url, hosts=request.hosts),
        data=request.body,
        headers=request.headers,
        method=request.method,
    )
    try:
        with opener.open(req, timeout=timeout) as resp:
            return HttpResponse(status=resp.status, body=resp.read())
    except urllib.error.HTTPError as exc:
        # Stripe puts a structured error in the body; keep it, it is the
        # difference between "card declined" and "your key is wrong".
        return HttpResponse(status=exc.code, body=exc.read())
    except urllib.error.URLError as exc:
        raise StripeError(f"could not reach Stripe: {exc.reason}") from None

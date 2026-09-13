"""Ask a distributor whether a part number exists.

The sourcing model names a manufacturer and a part number per row; until
this module existed nothing could check either, so every MPN stayed
``"proposed"``. This is the check: one distributor, Mouser, through its
Search API v2, behind the same kind of seam :mod:`meetings.meet` and
:mod:`googleapps.transport` use -- a :class:`Transport` Protocol, a urllib
implementation, a recorded transport in the tests -- so the suite never
opens a socket.

What a hit means, exactly: Mouser's part-number search, asked for an exact
match, returned a part whose ``ManufacturerPartNumber`` equals the proposed
one (case-insensitively). That is enough to call the MPN **verified**: a
distributor stocks a part by that number. It is *not* a check that the
package matches the land pattern on the board -- Mouser's ``Package / Case``
attribute is free text ("SOT-223-4", "SOT-223-3") that cannot be compared
to a footprint name mechanically, so it is passed on in the note for the
engineer to read and never used to downgrade a hit. A miss is information,
not an error: the part stays ``"proposed"`` with a note saying Mouser does
not list it.

The three verdicts are ``"verified"``, ``"unlisted"``, and
``"unavailable"``. The third covers everything that stopped the question
being answered -- a network failure, a non-2xx, a body that was not the
documented shape, an ``Errors`` entry from Mouser (an invalid key answers
that way) -- because none of those says whether the part exists, and none
may read as anything but unverified. :func:`verify_mpns` applies the
verdicts to BOM rows: an ``unlisted`` or ``unavailable`` row keeps
``"proposed"`` and carries the reason in ``verify_error``, in words,
never a quiet ``"none"``; the first ``unavailable`` stops the batch --
a dead API is not asked N times -- and every row it never asked about says
so too. With no ``MOUSER_API_KEY`` there is no verifier at all
(:func:`from_env` returns None): the rows stay ``"proposed"``, which is the
honest word for an MPN nobody checked.

The request surface, from Mouser's Search API v2 documentation:

* ``POST https://api.mouser.com/api/v2/search/partnumber?apiKey=<key>``
  with body ``{"SearchByPartRequest": {"mouserPartNumber": "<mpn>",
  "partSearchOptions": "Exact"}}``.
* Response ``{"Errors": [{"Code", "Message"}], "SearchResults":
  {"NumberOfResult": n, "Parts": [{"MouserPartNumber",
  "ManufacturerPartNumber", "Manufacturer", "Description",
  "ProductDetailUrl", "DataSheetUrl", "ProductAttributes": [{"AttributeName",
  "AttributeValue"}]}]}}``.

The key travels in the query string because that is where Mouser reads it,
which is why the transport refuses every redirect (a 3xx would carry it to
whatever host the redirect named) and why the host allowlist is enforced
at request construction, for every transport including the fakes. No
verdict, warning, or exception message ever carries the request URL.
"""

from __future__ import annotations

import dataclasses
import http.client
import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit

from ..sourcing import SourcingEntry

__all__ = [
    "DEFAULT_VERIFY_BUDGET_S",
    "DISTRIBUTOR",
    "MOUSER_API_KEY",
    "MOUSER_HOST",
    "MOUSER_SEARCH_URL",
    "MAX_RESPONSE_BYTES",
    "VERDICTS",
    "DistributorError",
    "HttpRequest",
    "HttpResponse",
    "MouserClient",
    "Transport",
    "Verdict",
    "Verify",
    "ensure_mouser_url",
    "from_env",
    "urllib_transport",
    "verify_mpns",
]

#: The distributor's name as it appears in notes and labels.
DISTRIBUTOR = "Mouser"

#: The environment variable that holds the Search API key.
MOUSER_API_KEY = "MOUSER_API_KEY"

#: The only host this module will ever address. Exact match -- a suffix
#: check would wave through ``api.mouser.com.evil.example``.
MOUSER_HOST = "api.mouser.com"

MOUSER_SEARCH_URL = f"https://{MOUSER_HOST}/api/v2/search/partnumber"

#: A part-number search for one exact MPN is a few kilobytes; a body past
#: this is not the documented response, whatever it is.
MAX_RESPONSE_BYTES = 1 << 20

#: The frozen verdict vocabulary. ``propose_sourcing`` raises on anything
#: else, the datasheet probe's rule.
VERDICTS: frozenset[str] = frozenset({"verified", "unlisted", "unavailable"})

#: Mouser's attribute name for the package, when the part carries one.
_PACKAGE_ATTRIBUTE = "package / case"

_USER_AGENT = "silkscreen/0.1"


class DistributorError(RuntimeError):
    """A transport failure, or a request refused before it was made.

    ``code`` is a short local reason (``network_error``, ``bad_host``);
    ``detail`` is safe to show and never contains the URL.
    """

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Verdict:
    """What the distributor said about one part number.

    ``status`` is one of :data:`VERDICTS`. On ``"verified"`` the
    distributor's own spelling of the manufacturer and the part number,
    its SKU and product page, and the package attribute when it lists one.
    On ``"unavailable"`` ``detail`` says why, in a sentence fit for a
    warning.
    """

    status: str
    manufacturer: str | None = None
    mpn: str | None = None
    sku: str | None = None
    url: str | None = None
    package: str | None = None
    detail: str = ""


class Verify(Protocol):
    """The seam :func:`~silkscreen.agents.sourcing.propose_sourcing` takes."""

    def __call__(self, manufacturer: str | None, mpn: str) -> Verdict: ...


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


class Transport(Protocol):
    def __call__(self, request: HttpRequest) -> HttpResponse: ...


def ensure_mouser_url(url: str) -> str:
    """Return ``url`` unchanged if it is https to :data:`MOUSER_HOST`.

    Raises:
        DistributorError: for any other scheme or host, before a transport
            sees the request, so no byte -- and no key -- can leave toward
            a host this module does not know.
    """
    parsed = urlsplit(url)
    if parsed.scheme != "https":
        raise DistributorError(
            "bad_host", f"refusing non-https URL scheme {parsed.scheme!r}"
        )
    if parsed.hostname != MOUSER_HOST:
        raise DistributorError(
            "bad_host", f"refusing to send to {parsed.hostname!r}: not {MOUSER_HOST}"
        )
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: a 3xx would re-send the key elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def urllib_transport(timeout_s: float = 10.0) -> Transport:
    """The real transport. A non-2xx is a response, not an exception --
    Mouser puts its reason in the body. The URL never enters an error."""

    opener = urllib.request.build_opener(_NoRedirect())

    def send(request: HttpRequest) -> HttpResponse:
        ensure_mouser_url(request.url)
        req = urllib.request.Request(
            request.url,
            data=request.body or None,
            headers=request.headers,
            method=request.method,
        )
        try:
            with opener.open(req, timeout=timeout_s) as resp:
                return HttpResponse(resp.status, resp.read(MAX_RESPONSE_BYTES + 1))
        except urllib.error.HTTPError as exc:
            with exc:
                return HttpResponse(exc.code, exc.read(MAX_RESPONSE_BYTES + 1))
        except urllib.error.URLError as exc:
            # ``reason`` is the socket's text (DNS, refused, timed out); it
            # never carries the URL.
            raise DistributorError("network_error", str(exc.reason)) from exc
        except (OSError, http.client.HTTPException) as exc:
            # A bare socket timeout, or a malformed/truncated HTTP exchange;
            # neither message carries the URL.
            raise DistributorError("network_error", str(exc) or "socket error") from exc

    return send


def _text(value: Any) -> str | None:
    """A stripped non-empty string, else None -- the sourcing parser's rule."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


class MouserClient:
    """A :class:`Verify` over Mouser's part-number search.

    ``transport`` defaults to :func:`urllib_transport`; the tests pass a
    recorded one, and :meth:`request` / :meth:`read` are separable so the
    exact bytes built and the exact parse of a body are each testable
    without the other.
    """

    def __init__(
        self,
        api_key: str,
        transport: Transport | None = None,
        *,
        timeout_s: float = 10.0,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("MouserClient needs a non-empty API key")
        self._api_key = api_key.strip()
        self._transport = (
            transport if transport is not None else urllib_transport(timeout_s)
        )

    def request(self, mpn: str) -> HttpRequest:
        """The exact request for one part number. The host check runs here
        too, so a wrong base URL is refused whatever transport follows."""
        url = ensure_mouser_url(
            f"{MOUSER_SEARCH_URL}?{urlencode({'apiKey': self._api_key})}"
        )
        body = json.dumps(
            {
                "SearchByPartRequest": {
                    "mouserPartNumber": mpn,
                    "partSearchOptions": "Exact",
                }
            }
        ).encode("utf-8")
        return HttpRequest(
            "POST",
            url,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": _USER_AGENT,
            },
            body=body,
        )

    @staticmethod
    def read(response: HttpResponse, mpn: str) -> Verdict:
        """The verdict in a response for ``mpn``. Never raises."""
        if len(response.body) > MAX_RESPONSE_BYTES:
            return Verdict(
                "unavailable",
                detail=f"{DISTRIBUTOR} answered with a body over "
                f"{MAX_RESPONSE_BYTES} bytes",
            )
        if response.status != 200:
            return Verdict(
                "unavailable", detail=f"{DISTRIBUTOR} answered HTTP {response.status}"
            )
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return Verdict(
                "unavailable",
                detail=f"{DISTRIBUTOR} answered with a body that was not JSON",
            )
        if not isinstance(payload, dict):
            return Verdict(
                "unavailable",
                detail=f"{DISTRIBUTOR} answered with a JSON {type(payload).__name__}, "
                "not the documented object",
            )
        errors = payload.get("Errors")
        if isinstance(errors, list) and errors:
            first = errors[0] if isinstance(errors[0], dict) else {}
            code = _text(first.get("Code")) or "an error"
            message = _text(first.get("Message"))
            why = f"{code}: {message}" if message else code
            return Verdict("unavailable", detail=f"{DISTRIBUTOR} answered {why}")
        results = payload.get("SearchResults")
        if not isinstance(results, dict):
            return Verdict(
                "unavailable",
                detail=f"{DISTRIBUTOR} answered without a SearchResults object",
            )
        parts = results.get("Parts")
        if not isinstance(parts, list):
            parts = []
        wanted = mpn.strip().casefold()
        for part in parts:
            if not isinstance(part, dict):
                continue
            listed = _text(part.get("ManufacturerPartNumber"))
            if listed is None or listed.casefold() != wanted:
                continue
            return Verdict(
                "verified",
                manufacturer=_text(part.get("Manufacturer")),
                mpn=listed,
                sku=_text(part.get("MouserPartNumber")),
                url=_https_only(_text(part.get("ProductDetailUrl"))),
                package=_package_of(part.get("ProductAttributes")),
            )
        return Verdict("unlisted")

    def __call__(self, manufacturer: str | None, mpn: str) -> Verdict:
        try:
            response = self._transport(self.request(mpn))
        except DistributorError as exc:
            return Verdict(
                "unavailable",
                detail=f"{DISTRIBUTOR} could not be reached ({exc.detail or exc.code})",
            )
        return self.read(response, mpn)


def _https_only(url: str | None) -> str | None:
    """A product page a client may link to: http(s) only, else nothing."""
    if url is None:
        return None
    return url if urlsplit(url).scheme.lower() in ("http", "https") else None


def _package_of(attributes: Any) -> str | None:
    if not isinstance(attributes, list):
        return None
    for attribute in attributes:
        if not isinstance(attribute, dict):
            continue
        name = _text(attribute.get("AttributeName"))
        if name is not None and name.casefold() == _PACKAGE_ATTRIBUTE:
            return _text(attribute.get("AttributeValue"))
    return None


def from_env(
    environ: Mapping[str, str] | None = None, transport: Transport | None = None
) -> Verify | None:
    """The verifier for this process: a :class:`MouserClient` when
    :data:`MOUSER_API_KEY` is set, None otherwise -- no key, no distributor,
    and the rows stay ``"proposed"``."""
    env = os.environ if environ is None else environ
    key = (env.get(MOUSER_API_KEY) or "").strip()
    if not key:
        return None
    return MouserClient(key, transport)


#: Wall-clock seconds :func:`verify_mpns` spends on one board, the datasheet
#: probes' figure: the order step waits on this too.
DEFAULT_VERIFY_BUDGET_S = 20.0


def verify_mpns(
    rows: Sequence[SourcingEntry],
    verify: Verify,
    *,
    budget_s: float = DEFAULT_VERIFY_BUDGET_S,
) -> tuple[list[SourcingEntry], list[str]]:
    """Ask ``verify`` about every ``"proposed"`` row; return the rows and
    the warnings, in order. Lookups run one at a time and stop once
    ``budget_s`` has elapsed, the rows after that marked "not asked".

    A ``"verified"`` verdict makes the row ``mpn_status == "verified"`` with
    the distributor's name, SKU and product page on it (and its spelling of
    the manufacturer when the model gave none). ``"unlisted"`` keeps the row
    ``"proposed"`` with ``verify_error`` saying the distributor does not list
    the number. The first ``"unavailable"`` -- or a verifier that raised --
    keeps its row ``"proposed"`` with the reason as ``verify_error``, adds
    one warning, and stops: every later proposed row is marked
    ``verify_error`` "not asked" with the same reason rather than left
    looking unchecked by choice. Rows with no part number are untouched.

    Raises:
        ValueError: the verifier answered outside :data:`VERDICTS` -- a
            programming error, the datasheet probe's rule.
    """
    out: list[SourcingEntry] = []
    warnings: list[str] = []
    stopped: str | None = None
    started = time.monotonic()
    for row in rows:
        if row.mpn_status != "proposed" or row.mpn is None:
            out.append(row)
            continue
        if stopped is None and time.monotonic() - started > budget_s:
            stopped = f"the {budget_s:g} s verification budget ran out"
            warnings.append(f"part numbers were not verified past {row.ref}: {stopped}")
        if stopped is not None:
            out.append(
                dataclasses.replace(
                    row, verify_error=f"{DISTRIBUTOR} was not asked: {stopped}"
                )
            )
            continue
        try:
            verdict = verify(row.manufacturer, row.mpn)
        except Exception as exc:  # noqa: BLE001 -- reported on the row
            verdict = Verdict(
                "unavailable",
                detail=f"{DISTRIBUTOR} lookup raised {type(exc).__name__}: "
                f"{str(exc)[:160]}",
            )
        if verdict.status not in VERDICTS:
            raise ValueError(
                f"the distributor verifier answered {verdict.status!r} for "
                f"{row.mpn}; expected one of {sorted(VERDICTS)}"
            )
        if verdict.status == "verified":
            out.append(
                dataclasses.replace(
                    row,
                    mpn_status="verified",
                    manufacturer=row.manufacturer or verdict.manufacturer,
                    distributor=DISTRIBUTOR,
                    distributor_sku=verdict.sku,
                    distributor_url=verdict.url,
                    verify_error=None,
                )
            )
        elif verdict.status == "unlisted":
            out.append(
                dataclasses.replace(
                    row, verify_error=f"{DISTRIBUTOR} does not list {row.mpn}"
                )
            )
        else:
            stopped = verdict.detail or f"{DISTRIBUTOR} could not answer"
            out.append(dataclasses.replace(row, verify_error=stopped))
            warnings.append(
                f"part numbers were not verified past {row.ref}: {stopped}"
            )
    return out, warnings

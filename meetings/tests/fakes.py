"""Offline stand-in for the Meet HTTP transport.

The whole point of the :class:`~meetings.meet.Transport` protocol is that no
test in this package ever opens a socket. :class:`FakeTransport` is the
recorded half of that seam, the same role ``ScriptedModel`` plays for the
agents layer: it answers from a script and remembers exactly what it was
asked, so a test can assert on the *request* -- the Authorization header, the
page token, the URL -- and not only on the parsed result.

A URL this transport has no script for is an :class:`AssertionError`, never a
silent 404. A test that accidentally reaches for an unscripted endpoint should
fail loudly rather than exercise the client's error path by accident.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

__all__ = ["FakeTransport", "Request", "page", "paged"]


@dataclass(frozen=True)
class Request:
    """One recorded call. ``headers`` is a copy, not the live dict."""

    url: str
    headers: dict[str, str]

    @property
    def authorization(self) -> str:
        return self.headers.get("Authorization", "")

    @property
    def query(self) -> dict[str, str]:
        """The query string as a flat dict; ``{}`` when there is none."""
        _, _, raw = self.url.partition("?")
        if not raw:
            return {}
        out: dict[str, str] = {}
        for chunk in raw.split("&"):
            key, _, value = chunk.partition("=")
            out[key] = value
        return out


def page(key: str, items: list[dict], next_token: str | None = None) -> dict:
    """One list-response body: ``{key: items, nextPageToken: ...}``."""
    body: dict[str, Any] = {key: items}
    if next_token:
        body["nextPageToken"] = next_token
    return body


def paged(key: str, pages: list[list[dict]]) -> list[dict]:
    """Turn a list of item-batches into a chain of tokenised page bodies.

    Every page but the last carries a ``nextPageToken``, so scripting
    multi-page pagination is one call rather than hand-written tokens.
    """
    bodies = []
    for index, items in enumerate(pages):
        last = index == len(pages) - 1
        bodies.append(page(key, items, None if last else f"tok-{index + 1}"))
    return bodies


#: What a script entry may be: a dict (200 + JSON), raw bytes (200), a
#: ``(status, dict|bytes)`` pair, a list of any of those consumed one per
#: request, or a callable taking ``(url, headers)`` and returning one of them.
Response = Any


class FakeTransport:
    """A :class:`~meetings.meet.Transport` that answers from a dict.

    Keys are matched as **substrings** of the request URL -- rightmost match
    first, then longest -- so a script can say "anything containing
    ``/entries``" without spelling out the base URL and query string.
    """

    def __init__(self, routes: dict[str, Response] | None = None):
        self.routes: dict[str, Response] = dict(routes or {})
        self.requests: list[Request] = []
        #: Per-key cursor for list-valued scripts, so successive requests to
        #: the same endpoint walk the pages in order.
        self._cursors: dict[str, int] = {}

    # -- scripting --------------------------------------------------------

    def route(self, key: str, response: Response) -> FakeTransport:
        """Add or replace one script entry. Returns self so calls chain."""
        self.routes[key] = response
        self._cursors.pop(key, None)
        return self

    # -- assertions -------------------------------------------------------

    @property
    def urls(self) -> list[str]:
        return [request.url for request in self.requests]

    def calls_matching(self, fragment: str) -> list[Request]:
        return [r for r in self.requests if fragment in r.url]

    # -- the protocol -----------------------------------------------------

    def get(self, url: str, headers: dict[str, str]) -> tuple[int, bytes]:
        self.requests.append(Request(url=url, headers=dict(headers)))
        key = self._match(url)
        return _materialise(self._next(key, url, headers))

    def _match(self, url: str) -> str:
        matches = [k for k in self.routes if k in url]
        if not matches:
            raise AssertionError(
                f"FakeTransport has no scripted response for {url!r}; "
                f"scripted keys: {sorted(self.routes)}"
            )
        # Rightmost match wins, then longest. A transcript-entries URL
        # contains both "/transcripts" and "/entries"; the caller means the
        # one nearest the end, which is the resource actually being fetched.
        return max(matches, key=lambda k: (url.rindex(k), len(k)))

    def _next(self, key: str, url: str, headers: dict[str, str]) -> Response:
        entry = self.routes[key]
        if callable(entry):
            return entry(url, headers)
        if isinstance(entry, list):
            index = self._cursors.get(key, 0)
            if index >= len(entry):
                raise AssertionError(
                    f"FakeTransport ran out of scripted pages for {key!r} "
                    f"after {len(entry)} requests (asked for {url!r})"
                )
            self._cursors[key] = index + 1
            return entry[index]
        return entry


def _materialise(response: Response) -> tuple[int, bytes]:
    """Normalise a script entry into ``(status, body_bytes)``."""
    status = 200
    body = response
    if isinstance(response, tuple):
        status, body = response
    if isinstance(body, (dict, list)):
        body = json.dumps(body).encode("utf-8")
    elif isinstance(body, str):
        body = body.encode("utf-8")
    if not isinstance(body, bytes):
        raise AssertionError(f"cannot use {type(body).__name__} as a body")
    return status, body

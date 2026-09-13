"""A small Slack Web API client, and request-signature verification.

Stdlib only, for the same reason ``service/app.py`` is: this repository's
dependency list is short and honest, and the three Slack endpoints a bot needs
are three HTTP calls. ``slack_sdk`` would be a reasonable dependency; it is not
a necessary one.

Every network call goes through :class:`Transport`, so the tests exercise the
real request construction -- URL, headers, encoding, the two-step file upload --
against a recorded transport instead of the network.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

__all__ = [
    "SlackError",
    "SlackClient",
    "HttpRequest",
    "HttpResponse",
    "Transport",
    "urllib_transport",
    "verify_signature",
    "SIGNATURE_VERSION",
    "MAX_SIGNATURE_AGE_S",
]

API_ROOT = "https://slack.com/api/"

#: Signature version prefix. **CONFIRMED 2026-09-08** against Slack's own
#: open-source verifier, ``slackapi/python-slack-sdk``
#: ``slack_sdk/signature/__init__.py::SignatureVerifier.generate_signature``,
#: which builds ``f"v0:{timestamp}:{body}"`` and compares against
#: ``f"v0={request_hash}"``. Kept as a constant so a future ``v1`` is a
#: one-line change with a test rather than a grep.
SIGNATURE_VERSION = "v0"
#: **CONFIRMED** in the same file: ``SignatureVerifier.is_valid`` refuses when
#: ``abs(self.clock.now() - int(timestamp)) > 60 * 5``. Two-sided, like this
#: module's check. An older timestamp is a replay, not a slow network, and is
#: refused even when the signature itself is valid.
MAX_SIGNATURE_AGE_S = 60 * 5


class SlackError(RuntimeError):
    """Slack answered, and the answer was a failure.

    ``code`` is Slack's own ``error`` string (``channel_not_found``,
    ``not_in_channel``, ``invalid_auth``), which is what tells an operator
    whether the fix is an invite, a scope, or a new token.
    """

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


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
            raise SlackError(
                "bad_response", f"HTTP {self.status} was not JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise SlackError("bad_response", "expected a JSON object")
        return payload


class Transport(Protocol):
    def __call__(self, request: HttpRequest) -> HttpResponse: ...


def urllib_transport(timeout: float = 30.0) -> Transport:
    """The real transport. An HTTP error is a response, not an exception --
    Slack puts its own error string in the body of a 200 *and* of a 429."""

    def send(request: HttpRequest) -> HttpResponse:
        req = urllib.request.Request(
            request.url,
            data=request.body or None,
            headers=request.headers,
            method=request.method,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return HttpResponse(resp.status, resp.read())
        except urllib.error.HTTPError as exc:
            return HttpResponse(exc.code, exc.read())
        except urllib.error.URLError as exc:
            raise SlackError("network_error", str(exc.reason)) from exc

    return send


def verify_signature(
    signing_secret: str,
    *,
    timestamp: str,
    signature: str,
    body: bytes,
    now: float | None = None,
) -> bool:
    """True if this request really came from Slack, recently.

    Both halves matter. The HMAC proves the body was not tampered with; the age
    check is what stops a valid, captured request from being replayed at us
    later. Comparison is constant-time, and every failure mode -- missing
    header, unparseable timestamp, wrong digest -- returns False rather than
    raising, so the caller has exactly one branch to write.
    """
    if not signing_secret or not timestamp or not signature:
        return False
    try:
        sent_at = float(timestamp)
    except ValueError:
        return False
    current = time.time() if now is None else now
    if abs(current - sent_at) > MAX_SIGNATURE_AGE_S:
        return False

    base = b"%s:%s:%s" % (SIGNATURE_VERSION.encode(), timestamp.encode(), body)
    digest = hmac.new(signing_secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    expected = f"{SIGNATURE_VERSION}={digest}"
    return hmac.compare_digest(expected, signature)


class SlackClient:
    """The handful of Web API methods this bot uses."""

    def __init__(
        self,
        token: str,
        *,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 2,
    ):
        self._token = token
        self._transport = transport or urllib_transport()
        self._sleep = sleep
        self._max_retries = max_retries

    # -- plumbing ---------------------------------------------------------

    def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST a JSON API method, retrying only what is worth retrying."""
        body = json.dumps(
            {k: v for k, v in payload.items() if v is not None}
        ).encode("utf-8")
        request = HttpRequest(
            "POST",
            API_ROOT + method,
            {
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            body,
        )
        return self._send_api(request, method)

    def _send_api(self, request: HttpRequest, method: str) -> dict[str, Any]:
        for attempt in range(self._max_retries + 1):
            response = self._transport(request)
            if response.status == 429 and attempt < self._max_retries:
                self._sleep(self._retry_after(response))
                continue
            data = response.json()
            if data.get("ok"):
                return data
            code = str(data.get("error") or f"http_{response.status}")
            if code == "ratelimited" and attempt < self._max_retries:
                self._sleep(self._retry_after(response))
                continue
            raise SlackError(code, f"{method} failed")
        raise SlackError("ratelimited", f"{method} gave up after retries")

    @staticmethod
    def _retry_after(response: HttpResponse) -> float:
        # **KNOWN GAP, stated rather than implied.** Slack puts the wait on the
        # ``Retry-After`` *header* of a 429 (docs.slack.dev "Rate limits"), and
        # :class:`HttpResponse` deliberately carries only status and body, so
        # that header is not visible here. The ``retry_after`` body field this
        # reads is the Events API's ``app_rate_limited`` shape, not the Web
        # API's, so in practice against real Slack this always falls through to
        # the one-second floor. That is a slow retry, never a wrong one: the
        # loop gives up after ``max_retries`` and raises ``ratelimited``, which
        # is a named failure in the thread rather than a hot loop or a silent
        # drop. Reading the header would mean widening the Transport seam.
        try:
            return max(1.0, float(response.json().get("retry_after", 1)))
        except SlackError:
            return 1.0

    # -- messages ---------------------------------------------------------

    def post_message(
        self,
        channel: str,
        text: str,
        *,
        thread_ts: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
    ) -> str:
        """Post a message; returns its ``ts``, which is a thread handle.

        ``text`` is always sent even when ``blocks`` are, because it is what
        notifications and screen readers use.
        """
        data = self._call(
            "chat.postMessage",
            {
                "channel": channel,
                "text": text,
                "thread_ts": thread_ts,
                "blocks": blocks,
                "unfurl_links": False,
                "unfurl_media": False,
            },
        )
        return str(data.get("ts", ""))

    def update_message(
        self,
        channel: str,
        ts: str,
        text: str,
        *,
        blocks: list[dict[str, Any]] | None = None,
    ) -> None:
        """Edit a message in place -- how a progress line advances without
        turning a thread into a scrollback of near-identical posts."""
        self._call(
            "chat.update",
            {"channel": channel, "ts": ts, "text": text, "blocks": blocks},
        )

    def add_reaction(self, channel: str, ts: str, name: str) -> None:
        """React to a message. A failure here is cosmetic, so it is swallowed:
        losing an emoji must never lose a run."""
        with contextlib.suppress(SlackError):
            self._call(
                "reactions.add", {"channel": channel, "timestamp": ts, "name": name}
            )

    def remove_reaction(self, channel: str, ts: str, name: str) -> None:
        with contextlib.suppress(SlackError):
            self._call(
                "reactions.remove",
                {"channel": channel, "timestamp": ts, "name": name},
            )

    # -- files ------------------------------------------------------------

    def upload_file(
        self,
        channel: str,
        filename: str,
        content: bytes,
        *,
        thread_ts: str | None = None,
        title: str | None = None,
        initial_comment: str | None = None,
    ) -> str:
        """Upload one file into a channel (and thread), returning its file id.

        This is Slack's three-step external upload: ask for a URL, POST the
        bytes at it, then tell Slack where the finished file belongs. The old
        one-shot ``files.upload`` is retired, so the extra hops are not
        optional.

        Every step below was checked on 2026-09-08 against Slack's own
        open-source client, ``slackapi/python-slack-sdk``
        (``slack_sdk/web/client.py::files_upload_v2`` and
        ``slack_sdk/web/internal_utils.py::_upload_file_via_v2_url``), and
        against the published method pages. Two of them were wrong here, and
        both would have failed *quietly* rather than raising:

        * Step 1 was a ``GET``. ``files.getUploadURLExternal`` is documented as
          a ``POST`` ("POST requests accepting ``application/x-www-form-
          urlencoded`` or ``application/json``") and the SDK sends
          ``api_call("files.getUploadURLExternal", params=kwargs)``, which is a
          POST carrying those arguments in the query string. That is now what
          this sends, character for character.
        * Step 2 wrapped the bytes in a hand-built ``multipart/form-data``
          envelope under the field name ``file``. The SDK POSTs the **raw
          bytes with no headers at all** (``Request(method="POST", url=url,
          data=data, headers={})``), and step 1 was already told
          ``length = len(content)`` -- "Size in bytes of the file being
          uploaded". The envelope made the body larger than the length
          declared, and the field name matched neither the SDK (no field) nor
          the docs' own curl example (``-F filename=...``). The failure mode is
          a stored file that is the envelope rather than the board, which
          nothing here would have noticed: the upload answers 200 either way
          and the ``.kicad_pcb`` only fails when a person opens it.
        """
        if not content:
            raise SlackError("empty_file", f"{filename} had no content")

        query = urllib.parse.urlencode(
            {"filename": filename, "length": str(len(content))}
        )
        reserved = self._send_api(
            HttpRequest(
                "POST",
                f"{API_ROOT}files.getUploadURLExternal?{query}",
                {"Authorization": f"Bearer {self._token}"},
            ),
            "files.getUploadURLExternal",
        )
        upload_url = str(reserved.get("upload_url", ""))
        file_id = str(reserved.get("file_id", ""))
        if not upload_url or not file_id:
            raise SlackError("bad_response", "no upload URL was issued")

        # Raw bytes, no headers -- byte-for-byte the body whose length step 1
        # declared. See _upload_file_via_v2_url, cited above.
        posted = self._transport(HttpRequest("POST", upload_url, {}, content))
        if posted.status >= 300:
            raise SlackError("upload_failed", f"HTTP {posted.status} storing bytes")

        self._call(
            "files.completeUploadExternal",
            {
                "files": [{"id": file_id, "title": title or filename}],
                "channel_id": channel,
                "thread_ts": thread_ts,
                "initial_comment": initial_comment,
            },
        )
        return file_id

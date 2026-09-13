"""Offline fixtures. No key, no network, no Stripe account required."""

from __future__ import annotations

import json
from typing import Any

import pytest

from billing.config import BillingConfig
from billing.transport import HttpRequest, HttpResponse


class RecordingTransport:
    """A transport that records what was built and replays canned answers.

    The point is to exercise *real request construction* -- URL, method,
    headers, form encoding, idempotency key -- which is the half of an HTTP
    integration that a mocked client library never checks.
    """

    def __init__(self, responses: list[tuple[int, Any]] | None = None) -> None:
        self.requests: list[HttpRequest] = []
        self._responses = list(responses or [])

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        status, payload = self._responses.pop(0) if self._responses else (200, {})
        return HttpResponse(status=status, body=json.dumps(payload).encode())

    @property
    def last(self) -> HttpRequest:
        return self.requests[-1]

    def form(self) -> dict[str, str]:
        from urllib.parse import parse_qsl

        return dict(parse_qsl((self.last.body or b"").decode()))


@pytest.fixture
def config() -> BillingConfig:
    return BillingConfig(
        api_key="rk_test_0123456789abcdef",
        webhook_secret="whsec_testsecret",
        price_id="price_credits20",
        credit_mkcu_per_purchase=8888,
        success_url="kaleo://billing/success",
        cancel_url="kaleo://billing/cancel",
    )


@pytest.fixture
def transport() -> RecordingTransport:
    return RecordingTransport()

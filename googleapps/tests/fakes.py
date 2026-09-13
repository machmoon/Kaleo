"""Offline stand-ins: a recording Google transport, and a finished run.

The result here is built by hand rather than solved. A CP-SAT solve takes
seconds and proves nothing about card or MIME formatting, and hand-built
findings and routing let a test assert exact text -- which is the only way
to notice the honesty rules (unrouted nets named, blockers counted) being
broken by a formatter.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "engine"))

from silkscreen.agents.review import (
    Finding,
    ReviewReport,
    ReviewStatus,
    Severity,
)
from silkscreen.board import BoardResult, PlacedPart
from silkscreen.footprints import Footprint, Pad
from silkscreen.routing import RouteResult
from silkscreen.units import mm

from ..auth import save_token
from ..config import Config
from ..transport import HttpRequest, HttpResponse

__all__ = [
    "RecordingTransport",
    "config_with_token",
    "fake_board",
    "fake_result",
    "fake_route",
    "finding",
    "valid_token",
]

WEBHOOK = (
    "https://chat.googleapis.com/v1/spaces/AAAA/messages"
    "?key=k-secret-key&token=t-secret-token"
)


class RecordingTransport:
    """A transport that records requests and replays canned answers.

    ``responses`` maps a URL fragment to a payload; unmatched calls answer a
    generic success so a test asserting on one endpoint need not stub the
    others. The token endpoint has a useful default because nearly every
    Gmail/Calendar test path crosses it.
    """

    def __init__(self, responses: dict[str, Any] | None = None):
        self.requests: list[HttpRequest] = []
        self._responses = responses or {}

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        for fragment, payload in self._responses.items():
            if fragment in request.url:
                if isinstance(payload, HttpResponse):
                    return payload
                return HttpResponse(200, json.dumps(payload).encode())
        if "oauth2.googleapis.com/token" in request.url:
            return HttpResponse(
                200,
                json.dumps(
                    {
                        "access_token": "ya29.refreshed",
                        "expires_in": 3599,
                        "token_type": "Bearer",
                    }
                ).encode(),
            )
        return HttpResponse(200, b'{"id": "msg-1", "htmlLink": "", "status": "ok"}')

    def bodies(self, fragment: str) -> list[dict[str, Any]]:
        """Decoded JSON bodies of every request whose URL contains fragment."""
        return [
            json.loads(request.body.decode())
            for request in self.requests
            if fragment in request.url and request.body
        ]

    def called(self, fragment: str) -> bool:
        return any(fragment in request.url for request in self.requests)


def valid_token(now: float | None = None, **overrides: Any) -> dict[str, Any]:
    current = time.time() if now is None else now
    token = {
        "access_token": "ya29.live-token",
        "refresh_token": "1//refresh-token",
        "expires_at": current + 3000,
        "token_type": "Bearer",
    }
    token.update(overrides)
    return token


def config_with_token(tmp_path: Path, **overrides: Any) -> Config:
    """A config whose token file exists, valid for another ~50 minutes."""
    token_path = tmp_path / "google-token.json"
    save_token(token_path, valid_token(**overrides))
    return Config(
        client_id="client-id.apps.googleusercontent.com",
        client_secret="client-secret-value",
        chat_webhook=WEBHOOK,
        token_path=token_path,
        google_api_key="AIza-key",
    )


def _footprint(name: str, pads: int = 2) -> Footprint:
    return Footprint(
        name=name,
        pads=[
            Pad(
                number=str(index + 1),
                x_nm=mm(-0.6) if index == 0 else mm(0.6),
                y_nm=0,
                w_nm=mm(0.6),
                h_nm=mm(0.8),
                net=f"net{index}",
            )
            for index in range(pads)
        ],
        courtyard_w_nm=mm(1.0),
        courtyard_h_nm=mm(0.6),
    )


def fake_board(*, warnings: list[str] | None = None) -> BoardResult:
    return BoardResult(
        parts=[
            PlacedPart(
                ref="R1",
                footprint=_footprint("R_0603"),
                value="10k",
                x_nm=mm(2.0),
                y_nm=mm(3.0),
            ),
            PlacedPart(
                ref="C1",
                footprint=_footprint("C_0603"),
                value="100n",
                x_nm=mm(8.0),
                y_nm=mm(3.0),
            ),
        ],
        nets=["VCC", "GND"],
        width_nm=mm(20.0),
        height_nm=mm(12.0),
        solver_status="FEASIBLE",
        wirelength_nm=mm(31.5),
        warnings=list(warnings or []),
    )


def fake_route(*, unrouted: dict[str, str] | None = None) -> RouteResult:
    return RouteResult(
        routed=["VCC", "GND"],
        unrouted=dict(unrouted or {}),
    )


@dataclass
class FakeSpec:
    parts: int = 2
    nets: int = 2

    def part_count(self) -> int:
        return self.parts

    def net_count(self) -> int:
        return self.nets


@dataclass
class FakeResult:
    """The shape :class:`silkscreen.agents.PipelineResult` presents.

    Duplicated rather than constructed because a real one needs a validated
    CircuitSpec, and every attribute this package touches is listed here --
    which makes this file the record of exactly how much pipeline surface the
    integration depends on.
    """

    intent: str = "a 3.3V regulator"
    spec: FakeSpec = field(default_factory=FakeSpec)
    board: BoardResult = field(default_factory=fake_board)
    facts: list[Any] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    #: The critic's verdict. Defaults to a review that ran and answered;
    #: the tests about an unread critic pass a failed one explicitly.
    review: ReviewReport = field(
        default_factory=lambda: ReviewReport(status=ReviewStatus.OK)
    )
    attempts: list[Any] = field(default_factory=list)
    board_path: Path | None = None
    route: RouteResult | None = None

    @property
    def artifacts(self) -> list[Path]:
        return [p for p in (self.board_path,) if p]

    @property
    def blockers(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.BLOCKER]

    @property
    def repair_rounds(self) -> int:
        return max(0, len(self.attempts) - 1)

    def summary(self) -> str:
        w, h = self.board.size_mm
        return f"{self.spec.part_count()} parts · board {w:.2f} x {h:.2f} mm"


def finding(
    severity: Severity = Severity.BLOCKER,
    title: str = "VIN has no bulk capacitor",
    **kwargs: Any,
) -> Finding:
    return Finding(severity=severity, title=title, **{"detail": "", **kwargs})


def fake_result(**kwargs: Any) -> FakeResult:
    kwargs.setdefault("route", fake_route())
    return FakeResult(**kwargs)

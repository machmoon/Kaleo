"""Offline stand-ins: a recording Slack transport, and a finished run.

The board here is built by hand rather than solved. A CP-SAT solve takes
seconds and proves nothing about formatting or uploading, and hand-built
geometry lets a test assert an exact rectangle -- which is the only way to
notice a coordinate flip going the wrong way.
"""

from __future__ import annotations

import json
import sys
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
from silkscreen.units import mm

from ..slack import HttpRequest, HttpResponse

__all__ = [
    "RecordingTransport",
    "fake_board",
    "fake_result",
    "FakeSpec",
    "FakeResult",
    "finding",
]


class RecordingTransport:
    """A Slack transport that records requests and replays canned answers.

    Unmatched calls answer ``{"ok": true}``: a test asserting on
    ``chat.postMessage`` should not also have to stub ``reactions.add``.
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
        if request.url.startswith("https://slack.com/api/"):
            return HttpResponse(200, b'{"ok": true, "ts": "1700000000.000100"}')
        # An upload PUT/POST to the storage URL, which answers with no JSON.
        return HttpResponse(200, b"OK")

    def calls(self, method: str) -> list[dict[str, Any]]:
        """Decoded JSON bodies of every call to one API method."""
        out = []
        for request in self.requests:
            if request.url.endswith(method) and request.body:
                out.append(json.loads(request.body.decode()))
        return out

    def called(self, method: str) -> bool:
        return any(method in request.url for request in self.requests)


def _footprint(name: str, pads: int = 2) -> Footprint:
    half_w, half_h = mm(1.0), mm(0.6)
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
        courtyard_w_nm=half_w,
        courtyard_h_nm=half_h,
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
                rotated=True,
            ),
        ],
        nets=["VCC", "GND"],
        width_nm=mm(20.0),
        height_nm=mm(12.0),
        solver_status="FEASIBLE",
        wirelength_nm=mm(31.5),
        warnings=list(warnings or []),
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
class FakeRoute:
    """The shape `silkscreen.routing.RouteResult` presents to this layer.

    `unrouted` maps a net name to the router's own reason, verbatim -- the
    contract is that a net it could not finish is named, not silently left
    as ratsnest.
    """

    unrouted: dict[str, str] = field(default_factory=dict)
    routed: tuple[str, ...] = ()
    tracks: int = 0
    vias: int = 0


@dataclass
class FakeResult:
    """The shape :class:`silkscreen.agents.PipelineResult` presents.

    Duplicated rather than constructed because a real one needs a validated
    CircuitSpec, and every attribute the Slack layer touches is listed here --
    which makes this file the record of exactly how much pipeline surface the
    bot depends on.
    """

    intent: str = "a 3.3V regulator"
    spec: FakeSpec = field(default_factory=FakeSpec)
    board: BoardResult = field(default_factory=fake_board)
    facts: list[Any] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    #: The critic's verdict. Defaults to a review that ran and answered,
    #: because that is what most of these tests are about; the tests that
    #: care about a failed or skipped critic pass one in explicitly.
    review: ReviewReport = field(
        default_factory=lambda: ReviewReport(status=ReviewStatus.OK)
    )
    attempts: list[Any] = field(default_factory=list)
    board_path: Path | None = None
    #: What the router finished. Defaults to a fully routed board, because a
    #: run leaves one unless routing was turned off -- `runner.py` calls
    #: `generate_pcb` with the default `route=True`, so `PipelineResult.route`
    #: is always populated in production. The absence of this field here is
    #: why the card announced "Board ready" over a ratsnest for so long: the
    #: fakes could not express the failure the card had to rank.
    route: FakeRoute | None = field(default_factory=lambda: FakeRoute())

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
    return FakeResult(**kwargs)

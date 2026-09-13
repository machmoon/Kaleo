"""One run through the real pipeline, with only the model and Google stubbed.

Every other test in this package stubs the pipeline, which is right for
testing formatting but proves nothing about whether the integration can
actually drive the engine. This file does not stub it: a ``ScriptedModel``
supplies the circuit, the real proposal, validation, footprint generation,
CP-SAT placement, board emission and routing all run, and what lands in the
fake Gmail transport is a board file the engine genuinely produced.

The last test is the repository's round-trip property -- the written board
reparses with every part -- asserted on the bytes recovered from the ``raw``
field of the Gmail request, i.e. on exactly what would have left the machine.
"""

from __future__ import annotations

import base64
import email
import email.policy
import json

import pytest
from silkscreen.agents import ScriptedModel
from silkscreen.kicad import extract_parts, load_board

from googleapps import chat, gmail
from googleapps.config import Config
from googleapps.runner import email_body, run_pipeline
from googleapps.tests.fakes import WEBHOOK, RecordingTransport

# The same circuit the engine's own pipeline tests use: a regulator and a
# motor driver, six parts once the passives are placed.
CIRCUIT = {
    "devices": {
        "AMS1117-3.3": {"pins": {"GND": "1", "VOUT": "2", "VIN": "3"}},
        "DRV8837": {
            "pins": {"IN1": "1", "IN2": "2", "VM": "3", "GND": "4",
                     "OUT1": "5", "OUT2": "6", "VCC": "7", "nSLEEP": "8"},
        },
    },
    "passives": {
        "c_in": {"type": "capacitor", "value": "22uF"},
        "c_out": {"type": "capacitor", "value": "22uF"},
        "c_dec": {"type": "capacitor", "value": "100nF"},
        "r_sleep": {"type": "resistor", "value": "10k"},
    },
    "nets": {
        "VIN": ["AMS1117-3.3.VIN", "c_in.1", "DRV8837.VM"],
        "GND": ["AMS1117-3.3.GND", "DRV8837.GND", "c_in.2", "c_out.2", "c_dec.2"],
        "+3V3": ["AMS1117-3.3.VOUT", "DRV8837.VCC", "c_out.1", "c_dec.1",
                 "r_sleep.1"],
        "SLEEP": ["DRV8837.nSLEEP", "r_sleep.2"],
        "MOT": ["DRV8837.OUT1", "DRV8837.IN1"],
    },
}


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    """One real run, shared by every assertion below.

    Module-scoped because it contains a CP-SAT solve and a routing pass;
    re-running them per assertion would add seconds for no extra coverage.
    """
    workdir = tmp_path_factory.mktemp("googleapps-run")
    config = Config(google_api_key="offline")
    outcome = run_pipeline(
        config,
        "a 3.3V motor driver",
        workdir / "board.kicad_pcb",
        review=False,
        time_limit_s=10.0,
        model_factory=lambda: ScriptedModel(responses=[json.dumps(CIRCUIT)]),
    )

    transport = RecordingTransport()
    gmail.send_run_email(
        "ya29.offline",
        to=["team@example.com"],
        subject="silkscreen run",
        body=email_body(outcome),
        board_path=outcome.result.board_path,
        transport=transport,
    )
    chat.post_run_card(
        WEBHOOK,
        outcome.result,
        transport=transport,
        stage_lines=outcome.stage_lines,
        duration_s=outcome.duration_s,
    )
    return outcome, transport


def emailed_board(transport) -> bytes:
    body = json.loads(
        next(r for r in transport.requests if "gmail" in r.url).body
    )
    raw = base64.urlsafe_b64decode(body["raw"].encode("ascii"))
    message = email.message_from_bytes(raw, policy=email.policy.default)
    (attachment,) = list(message.iter_attachments())
    assert attachment.get_filename() == "board.kicad_pcb"
    return attachment.get_content()


def test_the_engine_actually_placed_a_board(run):
    outcome, _ = run
    assert len(outcome.result.board.parts) == 6
    status = outcome.result.board.solver_status.upper()
    assert status in ("OPTIMAL", "FEASIBLE", "FALLBACK")
    # The stage log ticked real stages, not a template.
    assert any(line.startswith("place:") for line in outcome.stage_lines)


def test_a_real_kicad_pcb_reached_the_fake_gmail(run):
    _, transport = run
    text = emailed_board(transport).decode("utf-8")
    assert text.startswith("(kicad_pcb")
    assert "footprint" in text
    # The board outline: without it, edge constraints mean nothing.
    assert "Edge.Cuts" in text


def test_the_emailed_board_reparses_with_every_part(run, tmp_path):
    """The repository's round-trip property, asserted on what left the code."""
    _, transport = run
    path = tmp_path / "received.kicad_pcb"
    path.write_bytes(emailed_board(transport))
    assert len(extract_parts(load_board(path))) == 6


def test_the_chat_card_tells_the_routing_truth(run):
    """Whatever the router managed, the card must not overstate it."""
    outcome, transport = run
    card = next(
        r for r in transport.requests if "chat.googleapis" in r.url
    ).body.decode("utf-8")
    route = outcome.result.route
    assert route is not None
    for net in route.unrouted:
        assert f"unrouted {net}" in card
    if not route.unrouted:
        assert "unrouted" not in card.lower()
    assert f"{len(route.routed)}/{len(route.routed) + len(route.unrouted)}" in card


def test_the_email_body_matches_the_run_it_describes(run):
    outcome, transport = run
    body = json.loads(
        next(r for r in transport.requests if "gmail" in r.url).body
    )
    raw = base64.urlsafe_b64decode(body["raw"].encode("ascii"))
    message = email.message_from_bytes(raw, policy=email.policy.default)
    text = message.get_body(("plain",)).get_content()
    assert outcome.result.summary().splitlines()[0] in text
    for net in (outcome.result.route.unrouted if outcome.result.route else {}):
        assert net in text

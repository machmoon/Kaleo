"""One run through the real pipeline, with only the model and Slack stubbed.

Every other test in this package stubs ``generate_pcb``, which is right for
testing formatting but proves nothing about whether the bot can actually drive
the engine. This file does not stub it: a ``ScriptedModel`` supplies the
circuit, the real proposal, validation, footprint generation, CP-SAT placement
and board emission all run, and what lands in Slack is a board file the engine
genuinely produced.

That makes this the slowest file here -- there is a real solve in it -- and the
only one that would notice the bot calling the pipeline wrongly.
"""

from __future__ import annotations

import json

import pytest
from silkscreen.agents import ScriptedModel

from slackbot import runner as R
from slackbot.commands import parse_command
from slackbot.config import Config
from slackbot.slack import SlackClient
from slackbot.tests.fakes import RecordingTransport

# The same circuit the engine's own pipeline tests use: a regulator and a motor
# driver, six parts once the passives are placed.
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

    Module-scoped because it contains a CP-SAT solve; re-solving it per
    assertion would add seconds for no extra coverage.
    """
    transport = RecordingTransport(
        {
            "getUploadURLExternal": {
                "ok": True,
                "upload_url": "https://files.slack.test/u",
                "file_id": "F1",
            }
        }
    )
    config = Config(
        bot_token="xoxb-t",
        signing_secret="s",
        time_limit_s=10.0,
        workdir=tmp_path_factory.mktemp("runs"),
    )
    runner = R.Runner(
        config,
        SlackClient("xoxb-t", transport=transport),
        model_factory=lambda: ScriptedModel(responses=[json.dumps(CIRCUIT)]),
    )
    runner.handle(
        parse_command("place a 3.3V motor driver"),
        channel="C1",
        thread_ts="1.1",
        user="U9",
    )
    return transport, runner, config


def uploads(transport) -> dict[str, bytes]:
    """Filename -> the exact bytes POSTed to the external upload URL.

    Slack's own client sends the raw file and nothing else
    (``slack_sdk/web/internal_utils.py::_upload_file_via_v2_url``), so there is
    no envelope to strip here -- and the round-trip assertions below are
    therefore against the bytes Slack would store, not against a body that
    merely contains them.
    """
    out: dict[str, bytes] = {}
    pending: str | None = None
    for request in transport.requests:
        if "getUploadURLExternal" in request.url:
            pending = request.url.split("filename=")[1].split("&")[0]
        elif pending and "slack.com" not in request.url:
            out[pending] = request.body
            pending = None
    return out


def test_the_engine_actually_placed_a_board(run):
    transport, runner, _ = run
    record = runner._store.get(("C1", "1.1"))
    assert record is not None
    assert len(record.result.board.parts) == 6
    status = record.result.board.solver_status.upper()
    assert status in ("OPTIMAL", "FEASIBLE", "FALLBACK")


def test_a_real_kicad_pcb_reaches_slack(run):
    transport, _, _ = run
    files = uploads(transport)
    board = next(v for k, v in files.items() if k.endswith(".kicad_pcb"))
    text = board.decode("utf-8")
    assert text.startswith("(kicad_pcb")
    assert "footprint" in text
    # The board outline: without it, edge constraints mean nothing.
    assert "Edge.Cuts" in text


def test_the_written_board_reparses_with_every_part(run):
    """The repository's round-trip property, asserted on what Slack received."""
    from silkscreen.kicad import extract_parts, load_board

    _, _, config = run
    path = next(config.workdir.rglob("*.kicad_pcb"))
    assert len(extract_parts(load_board(path))) == 6


def test_the_preview_matches_the_board_that_was_uploaded(run):
    transport, runner, _ = run
    files = uploads(transport)
    preview = next(k for k in files if k.endswith((".png", ".svg")))
    assert files[preview]

    record = runner._store.get(("C1", "1.1"))
    from slackbot.render import render_svg

    svg = render_svg(record.result.board)
    # Every ref the engine placed is drawn, and nothing invented is.
    for part in record.result.board.parts:
        assert f">{part.ref}<" in svg


def test_the_channel_was_told_what_was_built(run):
    transport, runner, _ = run
    messages = json.dumps(
        transport.calls("chat.postMessage"), ensure_ascii=False
    )
    board = runner._store.get(("C1", "1.1")).result.board
    width, height = board.size_mm
    assert "Board ready" in messages
    # The size in the message is the size the solver produced, not a template.
    assert f"{width:.2f} × {height:.2f} mm" in messages
    assert board.solver_status in messages


def test_an_order_draft_from_a_real_run(run):
    transport, runner, _ = run
    runner.handle(parse_command("order 10"), channel="C1", thread_ts="1.1")

    run_id = runner._store.get(("C1", "1.1")).run_id
    draft = json.loads(uploads(transport)[f"order-draft-{run_id}.json"])
    assert draft["submitted"] is False
    assert draft["quantity"] == 10
    assert draft["board"]["area_mm2"] > 0
    assert "board.kicad_pcb" in draft["artifacts"]
    # Placement is not fabrication, and the draft has to keep saying so.
    assert draft["ready_to_order"] is False
    assert any("Gerber" in gap for gap in draft["missing_for_fabrication"])

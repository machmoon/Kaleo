"""MCP: the protocol itself, not just the tools behind it."""

import io
import json
from types import SimpleNamespace

import pytest
import silkscreen.mcp.server as mcp_server
from silkscreen.mcp.server import (
    METHOD_NOT_FOUND,
    PROTOCOL_VERSION,
    TOOLS,
    Server,
    handle,
)
from silkscreen.spice.simulators import NgspiceSimulator

CIRCUIT = {
    "devices": {"U1": {"pins": {"1": "GND", "2": "VOUT", "3": "VIN"}}},
    "passives": {
        "C1": {"type": "capacitor", "value": "10uF"},
        "C2": {"type": "capacitor", "value": "100nF"},
    },
    "nets": {
        "VIN": ["U1.3", "C1.1"],
        "GND": ["U1.1", "C1.2", "C2.2"],
        "VOUT": ["U1.2", "C2.1"],
    },
}


def rpc(method, params=None, req_id=1):
    return handle(
        {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
    )


def call(name, arguments=None):
    return rpc("tools/call", {"name": name, "arguments": arguments or {}})


def payload(response):
    """The JSON a successful tool call carried back."""
    content = response["result"]["content"][0]["text"]
    return json.loads(content)


def test_initialize_reports_protocol_and_server():
    result = rpc("initialize")["result"]
    assert result["protocolVersion"] == PROTOCOL_VERSION
    assert result["serverInfo"]["name"] == "silkscreen"
    assert "tools" in result["capabilities"]


def test_initialized_notification_gets_no_reply():
    assert handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_ping():
    assert rpc("ping")["result"] == {}


def test_tools_list_is_well_formed():
    tools = rpc("tools/list")["result"]["tools"]
    assert len(tools) == len(TOOLS)
    for tool in tools:
        assert tool["name"] and tool["description"]
        assert tool["inputSchema"]["type"] == "object"


def test_unknown_method_is_a_jsonrpc_error():
    err = rpc("does/not/exist")["error"]
    assert err["code"] == METHOD_NOT_FOUND


def test_wrong_jsonrpc_version_is_rejected():
    assert "error" in handle({"jsonrpc": "1.0", "id": 1, "method": "ping"})


def test_unknown_tool_is_a_jsonrpc_error():
    assert "error" in call("no_such_tool")


def test_response_id_matches_the_request():
    assert rpc("ping", req_id="abc-123")["id"] == "abc-123"


def test_validate_circuit_accepts_a_good_circuit():
    body = payload(call("validate_circuit", CIRCUIT))
    assert body == {"valid": True, "devices": 1, "passives": 2, "nets": 3}


def test_validate_circuit_reports_every_error_at_once():
    bad = {
        "devices": {"U1": {"pins": {"1": "GND"}}},
        "nets": {"GND": ["U1.1", "U1.99"], "FLOAT": ["U1.1"]},
    }
    body = payload(call("validate_circuit", bad))
    assert body["valid"] is False
    assert len(body["errors"]) >= 2, "all failures, not just the first"


def test_generate_footprint_returns_two_pads_for_a_chip_passive():
    body = payload(call("generate_footprint", {"package": "0805"}))
    assert body["name"] == "C_0805"
    assert len(body["pads"]) == 2
    assert body["pads"][0]["x_mm"] == -body["pads"][1]["x_mm"], "symmetric"
    assert body["courtyard_mm"][0] > 0


def test_generate_footprint_refuses_an_unknown_package():
    response = call("generate_footprint", {"package": "0201"})
    assert response["result"]["isError"] is True


def test_build_board_places_every_part():
    body = payload(call("build_board", {"circuit": CIRCUIT, "time_limit_s": 5}))
    assert [p["ref"] for p in body["parts"]] == ["U1", "C1", "C2"]
    assert body["board_mm"][0] > 0 and body["board_mm"][1] > 0


def test_emit_kicad_pcb_returns_a_parseable_board():
    body = payload(call("emit_kicad_pcb", {"circuit": CIRCUIT, "time_limit_s": 5}))
    text = body["kicad_pcb"]
    assert text.startswith("(kicad_pcb")
    assert text.count("(footprint") == 3
    assert body["bytes"] == len(text.encode())


def test_place_parts_returns_a_placement_per_part():
    body = payload(
        call(
            "place_parts",
            {
                "parts": [
                    {"ref": "U1", "width_mm": 10, "height_mm": 10},
                    {"ref": "C1", "width_mm": 2, "height_mm": 1.2},
                ],
                "time_limit_s": 3,
            },
        )
    )
    assert {p["ref"] for p in body["placements"]} == {"U1", "C1"}


def test_place_parts_rejects_an_empty_list():
    assert call("place_parts", {"parts": []})["result"]["isError"] is True


def test_a_bad_circuit_is_an_error_result_not_a_crash():
    response = call("build_board", {"circuit": {"nets": {"N": ["U9.1"]}}})
    assert response["result"]["isError"] is True


@pytest.mark.parametrize("name", [t["name"] for t in TOOLS])
def test_every_advertised_tool_is_dispatchable(name):
    assert "error" not in call(name, {}) or "unknown tool" not in str(call(name, {}))


def test_stdio_round_trip():
    """The transport, end to end, over real streams."""
    lines = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        "",
        "not json at all",
    ]
    out = io.StringIO()
    Server(stdin=io.StringIO("\n".join(lines)), stdout=out).serve_forever()
    replies = [json.loads(line) for line in out.getvalue().splitlines()]

    assert [r.get("id") for r in replies] == [1, 2, None]
    assert replies[1]["result"]["tools"]
    assert replies[2]["error"]["message"].startswith("invalid JSON")


# --------------------------------------------------------------------------
# simulation tools
# --------------------------------------------------------------------------

#: A resistive divider, the simplest circuit with a checkable DC answer.
SIM_CIRCUIT = {
    "passives": {
        "Rtop": {"type": "resistor", "value": "10k"},
        "Rbot": {"type": "resistor", "value": "10k"},
        "Cbyp": {"type": "capacitor", "value": "100nF"},
    },
    "nets": {
        "VIN": ["Rtop.1", "Cbyp.1"],
        "VMID": ["Rtop.2", "Rbot.1"],
        "GND": ["Rbot.2", "Cbyp.2"],
    },
}

SIM_BENCH = {
    "analysis": {"kind": "op"},
    "sources": [
        {"name": "V1", "positive": "VIN", "negative": "GND", "dc": 5.0}
    ],
}

needs_ngspice = pytest.mark.skipif(
    not NgspiceSimulator().is_available(),
    reason="ngspice is not installed on this machine",
)


def test_spice_capabilities_lists_measurement_kinds_without_local_paths(monkeypatch):
    monkeypatch.setattr(
        mcp_server,
        "available_simulators",
        lambda: [SimpleNamespace(name="ngspice", executable="C:/private/ngspice")],
    )
    body = payload(call("spice_capabilities"))
    assert "rise_time" in body["measurement_kinds"]
    assert "within" in body["operators"]
    assert body["simulators"] == ["ngspice"]
    assert "private" not in json.dumps(body)


@pytest.mark.parametrize(
    "testbench",
    [
        {**SIM_BENCH, "title": "demo\n.control"},
        {**SIM_BENCH, "options": ["reltol=1e-3\n.control"]},
        {
            **SIM_BENCH,
            "models": {
                "Rtop": {
                    "kind": "model",
                    "name": "R",
                    "text": ".control\nshell echo injected\n.endc",
                }
            },
        },
        {
            "analysis": {"kind": "tran", "step": 1e-6, "stop": 1e-3},
            "sources": [
                {
                    "name": "V1",
                    "positive": "VIN",
                    "negative": "GND",
                    "dc": 5.0,
                    "transient": "PULSE(0 5 0 1n 1n 1u 2u)\n.control",
                }
            ],
        },
    ],
    ids=["title", "options", "model-program", "raw-transient"],
)
def test_simulate_circuit_rejects_raw_spice_before_launch(
    monkeypatch, testbench
):
    launched = False

    def unexpected_launch(*args, **kwargs):
        nonlocal launched
        launched = True
        raise AssertionError("the simulator must not be launched")

    monkeypatch.setattr(mcp_server, "simulate_deck", unexpected_launch)
    body = payload(
        call(
            "simulate_circuit",
            {"circuit": SIM_CIRCUIT, "testbench": testbench},
        )
    )
    assert body["ok"] is False
    assert body["stage"] == "testbench"
    assert launched is False


@pytest.mark.parametrize(
    "extra",
    [
        {"unexpected": True},
        {"timeout_s": float("nan")},
        {"timeout_s": float("inf")},
        {"timeout_s": 121},
        {"max_points": 2001},
        {"max_points": 1.5},
        {"simulator": "hspice"},
    ],
    ids=[
        "unknown-field",
        "nan-timeout",
        "infinite-timeout",
        "long-timeout",
        "too-many-points",
        "fractional-points",
        "unknown-simulator",
    ],
)
def test_simulate_circuit_rejects_unsafe_controls_before_launch(
    monkeypatch, extra
):
    launched = False

    def unexpected_launch(*args, **kwargs):
        nonlocal launched
        launched = True
        raise AssertionError("the simulator must not be launched")

    monkeypatch.setattr(mcp_server, "simulate_deck", unexpected_launch)
    body = payload(
        call(
            "simulate_circuit",
            {"circuit": SIM_CIRCUIT, "testbench": SIM_BENCH, **extra},
        )
    )
    assert body["ok"] is False
    assert body["stage"] == "request"
    assert launched is False


def test_simulate_circuit_reports_an_invalid_circuit_without_simulating():
    body = payload(call("simulate_circuit", {"circuit": {"nets": {"N": ["U9.1"]}},
                                             "testbench": SIM_BENCH}))
    assert body["ok"] is False
    assert body["stage"] == "circuit"
    assert body["errors"]


def test_simulate_circuit_collects_testbench_problems():
    body = payload(
        call(
            "simulate_circuit",
            {
                "circuit": SIM_CIRCUIT,
                "testbench": {
                    "analysis": {"kind": "tran"},  # missing step and stop
                    "sources": [{"name": "V1", "positive": "NOPE",
                                 "negative": "GND", "dc": 5}],
                },
            },
        )
    )
    assert body["ok"] is False
    assert body["stage"] == "testbench"
    assert len(body["errors"]) >= 2


def test_simulate_circuit_rejects_an_unknown_measurement_kind():
    body = payload(
        call(
            "simulate_circuit",
            {
                "circuit": SIM_CIRCUIT,
                "testbench": SIM_BENCH,
                "assertions": [
                    {
                        "name": "x",
                        "measurement": {"kind": "vibes", "signal": "VMID"},
                        "op": "<",
                        "value": 1,
                    }
                ],
            },
        )
    )
    assert body["ok"] is False
    assert "vibes" in str(body["errors"])


@needs_ngspice
def test_simulate_circuit_returns_a_verdict_with_the_measured_number():
    body = payload(
        call(
            "simulate_circuit",
            {
                "circuit": SIM_CIRCUIT,
                "testbench": SIM_BENCH,
                "assertions": [
                    {
                        "name": "midpoint is half the supply",
                        "measurement": {"kind": "final", "signal": "VMID"},
                        "op": "within",
                        "value": 2.5,
                        "tolerance": 0.01,
                        "unit": "V",
                    },
                    {
                        "name": "midpoint is 3.3 V",
                        "measurement": {"kind": "final", "signal": "VMID"},
                        "op": "within",
                        "value": 3.3,
                        "tolerance": 0.01,
                        "unit": "V",
                    },
                ],
            },
        )
    )
    assert body["ok"] is True
    assert body["passed"] is False
    first, second = body["assertions"]
    assert first["passed"] is True
    assert first["measured"] == pytest.approx(2.5, rel=1e-6)
    assert second["passed"] is False
    assert "midpoint is 3.3 V" in body["summary"]


@needs_ngspice
def test_simulate_circuit_without_assertions_returns_waveform_summary():
    body = payload(
        call(
            "simulate_circuit",
            {"circuit": SIM_CIRCUIT, "testbench": SIM_BENCH},
        )
    )
    assert body["ok"] is True
    assert body["result"]["analysis"] == "op"
    assert "v(VMID)" in body["result"]["signals"]


def test_simulate_circuit_refuses_a_device_with_no_model():
    """An IC has no behaviour in the IR. Dropping it would simulate a different
    circuit and report success, so the tool must name it and stop."""
    body = payload(
        call(
            "simulate_circuit",
            {
                "circuit": {
                    "devices": {"U1": {"pins": {"A": "1", "B": "2"}}},
                    "passives": {"R1": {"type": "resistor", "value": "1k"}},
                    "nets": {"NA": ["U1.A", "R1.1"], "GND": ["U1.B", "R1.2"]},
                },
                "testbench": {
                    "analysis": {"kind": "op"},
                    "sources": [
                        {"name": "V1", "positive": "NA",
                         "negative": "GND", "dc": 5.0}
                    ],
                },
            },
        )
    )
    assert body["ok"] is False
    assert body["stage"] == "testbench"
    assert "U1" in str(body["errors"])

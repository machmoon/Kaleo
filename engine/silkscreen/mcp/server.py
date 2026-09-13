"""An MCP server over stdio.

The previous project shipped a directory named ``mcp/`` and called that an MCP
integration; there was no protocol anywhere in it. This is the actual thing:
JSON-RPC 2.0 over stdin/stdout, speaking ``initialize``, ``tools/list`` and
``tools/call`` per the Model Context Protocol, exposing the engine's useful
operations to any MCP client -- validation, placement, board and footprint
generation, and SPICE simulation. ``TOOLS`` is the list; do not restate its
length here, since it has already drifted once.

The transport is deliberately separable from the dispatch: :func:`handle` maps
one request dict to one response dict and never touches a stream, which is why
the protocol is testable without spawning a process.

Run it with::

    python -m silkscreen.mcp
"""

from __future__ import annotations

import json
import math
import sys
from collections.abc import Callable
from typing import Any

from ..board import build_board, emit_kicad_pcb
from ..footprints import CHIP_SIZES, UnsupportedPackage, chip_passive
from ..netlist import ValidationError, parse_circuit_spec
from ..packing import Part, pack
from ..spice import MEASUREMENT_KINDS, SpiceError, build_deck, check_all, simulate_deck
from ..spice.simulators import available_simulators
from ..spice.spec import REQUEST_SCHEMA as SPICE_REQUEST_SCHEMA
from ..spice.spec import assertions_from_dict, testbench_from_dict
from ..units import to_mm

__all__ = ["TOOLS", "Server", "handle", "main"]

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "silkscreen", "version": "0.1.0"}

# JSON-RPC 2.0 error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def _circuit_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "devices": {"type": "object"},
            "passives": {"type": "object"},
            "nets": {"type": "object"},
        },
        "required": ["nets"],
    }


TOOLS: list[dict[str, Any]] = [
    {
        "name": "validate_circuit",
        "description": (
            "Validate a circuit against the Silkscreen IR. Returns every error "
            "at once, not the first, so a caller can repair in one pass."
        ),
        "inputSchema": _circuit_schema(),
    },
    {
        "name": "build_board",
        "description": (
            "Turn a validated circuit into generated footprints, nets and a "
            "CP-SAT placement. Returns board size, wirelength and solver status."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "circuit": _circuit_schema(),
                "time_limit_s": {"type": "number", "default": 20.0},
            },
            "required": ["circuit"],
        },
    },
    {
        "name": "emit_kicad_pcb",
        "description": (
            "Emit a complete .kicad_pcb file for a circuit. No KiCad install "
            "and no footprint library required."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "circuit": _circuit_schema(),
                "time_limit_s": {"type": "number", "default": 20.0},
            },
            "required": ["circuit"],
        },
    },
    {
        "name": "place_parts",
        "description": (
            "Place bare rectangles with the CP-SAT packer. Sizes in millimetres."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "parts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "ref": {"type": "string"},
                            "width_mm": {"type": "number"},
                            "height_mm": {"type": "number"},
                        },
                        "required": ["ref", "width_mm", "height_mm"],
                    },
                },
                "clearance_mm": {"type": "number", "default": 0.25},
                "time_limit_s": {"type": "number", "default": 10.0},
            },
            "required": ["parts"],
        },
    },
    {
        "name": "generate_footprint",
        "description": (
            "Generate an IPC-7351 land pattern for a two-terminal chip package. "
            "Returns pads and courtyard in millimetres."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "package": {"type": "string", "enum": sorted(CHIP_SIZES)},
            },
            "required": ["package"],
        },
    },
    {
        "name": "simulate_circuit",
        "description": (
            "Simulate a circuit with SPICE and check it against a "
            "specification. This is the behavioural verifier: DRC says whether "
            "a board can be made, this says whether the circuit works. Give it "
            "a circuit, a testbench (sources plus one analysis) and a list of "
            "assertions; it returns a pass/fail verdict with the measured "
            "number and a signed margin beside every clause. Omit assertions "
            "to just read the waveforms back. Fails loudly: a missing device "
            "model, a probe on a net that does not exist, or a solver that "
            "will not converge is an error, never an empty result."
        ),
        "inputSchema": SPICE_REQUEST_SCHEMA,
    },
    {
        "name": "spice_capabilities",
        "description": (
            "Which SPICE simulators this machine can run, and every "
            "measurement kind an assertion may use. Call this before building "
            "a simulation request."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
]


def _text_result(payload: Any) -> dict[str, Any]:
    """MCP tool results are content blocks, not bare JSON."""
    return {
        "content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
        "isError": False,
    }


def _error_result(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _tool_validate_circuit(args: dict[str, Any]) -> dict[str, Any]:
    try:
        spec = parse_circuit_spec(args)
    except ValidationError as exc:
        return _text_result({"valid": False, "errors": list(exc.errors)})
    return _text_result(
        {
            "valid": True,
            "devices": len(spec.devices),
            "passives": len(spec.passives),
            "nets": len(spec.connections),
        }
    )


def _build(args: dict[str, Any]):
    spec = parse_circuit_spec(args.get("circuit") or {})
    return build_board(spec, time_limit_s=float(args.get("time_limit_s", 20.0)))


def _tool_build_board(args: dict[str, Any]) -> dict[str, Any]:
    result = _build(args)
    return _text_result(
        {
            "status": str(result.solver_status),
            "board_mm": [
                round(to_mm(result.width_nm), 3),
                round(to_mm(result.height_nm), 3),
            ],
            "wirelength_mm": (
                round(to_mm(result.wirelength_nm), 2)
                if result.wirelength_nm is not None
                else None
            ),
            "parts": [
                {"ref": p.ref, "footprint": p.footprint.name} for p in result.parts
            ],
            "warnings": list(result.warnings),
        }
    )


def _tool_emit_kicad_pcb(args: dict[str, Any]) -> dict[str, Any]:
    result = _build(args)
    text = emit_kicad_pcb(result)
    return _text_result(
        {
            "kicad_pcb": text,
            "bytes": len(text.encode()),
            "footprints": len(result.parts),
        }
    )


def _tool_place_parts(args: dict[str, Any]) -> dict[str, Any]:
    raw = args.get("parts") or []
    if not raw:
        return _error_result("place_parts needs at least one part")
    parts = [
        Part(
            width_nm=int(round(float(p["width_mm"]) * 1e6)),
            height_nm=int(round(float(p["height_mm"]) * 1e6)),
            ref=str(p["ref"]),
        )
        for p in raw
    ]
    result = pack(
        parts,
        clearance_nm=int(round(float(args.get("clearance_mm", 0.25)) * 1e6)),
        time_limit_s=float(args.get("time_limit_s", 10.0)),
    )
    return _text_result(
        {
            "status": str(result.status),
            "board_mm": [
                round(to_mm(result.board_width_nm), 3),
                round(to_mm(result.board_height_nm), 3),
            ],
            "placements": [
                {
                    "ref": p.ref,
                    "x_mm": round(to_mm(p.x_nm), 3),
                    "y_mm": round(to_mm(p.y_nm), 3),
                    "rotated": p.rotated,
                }
                for p in result.placements
            ],
            "warnings": list(result.warnings),
        }
    )


def _tool_generate_footprint(args: dict[str, Any]) -> dict[str, Any]:
    package = str(args.get("package", ""))
    try:
        fp = chip_passive(package)
    except UnsupportedPackage as exc:
        return _error_result(str(exc))
    return _text_result(
        {
            "name": fp.name,
            "pads": [
                {
                    "number": pad.number,
                    "x_mm": round(to_mm(pad.x_nm), 4),
                    "y_mm": round(to_mm(pad.y_nm), 4),
                    "width_mm": round(to_mm(pad.w_nm), 4),
                    "height_mm": round(to_mm(pad.h_nm), 4),
                }
                for pad in fp.pads
            ],
            "courtyard_mm": [
                round(to_mm(fp.courtyard_w_nm * 2), 4),
                round(to_mm(fp.courtyard_h_nm * 2), 4),
            ],
        }
    )


_SIMULATION_FIELDS = frozenset(
    {"circuit", "testbench", "assertions", "simulator", "timeout_s", "max_points"}
)
_MAX_SIMULATION_TIMEOUT_S = 120.0
_MAX_WAVEFORM_POINTS = 2000


def _is_finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _simulation_controls(
    args: Any,
) -> tuple[list[str], str | None, float, int]:
    """Validate resource controls before any external simulator is started."""
    if not isinstance(args, dict):
        return ["simulation request must be an object"], None, 60.0, 0

    errors = [
        f"simulation request: unknown field {key!r}"
        for key in sorted(set(args) - _SIMULATION_FIELDS)
    ]

    simulator = args.get("simulator")
    if simulator is not None and simulator not in ("ngspice", "ltspice"):
        errors.append("'simulator' must be 'ngspice' or 'ltspice'")

    timeout = args.get("timeout_s", 60.0)
    if (
        not _is_finite_number(timeout)
        or not 0 < timeout <= _MAX_SIMULATION_TIMEOUT_S
    ):
        errors.append(
            f"'timeout_s' must be a finite number greater than 0 and no more "
            f"than {_MAX_SIMULATION_TIMEOUT_S:g}"
        )
        timeout = 60.0

    max_points = args.get("max_points", 0)
    if (
        isinstance(max_points, bool)
        or not isinstance(max_points, int)
        or not 0 <= max_points <= _MAX_WAVEFORM_POINTS
    ):
        errors.append(
            f"'max_points' must be an integer between 0 and {_MAX_WAVEFORM_POINTS}"
        )
        max_points = 0

    return errors, simulator, float(timeout), max_points


def _tool_simulate_circuit(args: dict[str, Any]) -> dict[str, Any]:
    """Run one simulation and check it against a specification.

    Every failure comes back as an ``isError`` result carrying the simulator's
    own words. A tool that answered "no results" here would be read by a model
    as "the circuit does nothing", which is the one outcome this must never
    produce.
    """
    request_errors, simulator, timeout_s, max_points = _simulation_controls(args)
    if request_errors:
        return _text_result(
            {"ok": False, "stage": "request", "errors": request_errors}
        )

    try:
        spec = parse_circuit_spec(args.get("circuit") or {})
    except ValidationError as exc:
        return _text_result(
            {"ok": False, "stage": "circuit", "errors": list(exc.errors)}
        )

    try:
        bench = testbench_from_dict(args.get("testbench") or {})
        assertions = assertions_from_dict(args.get("assertions"))
        deck = build_deck(spec, bench)
    except SpiceError as exc:
        return _text_result(
            {
                "ok": False,
                "stage": "testbench",
                "errors": list(getattr(exc, "errors", [])) or [str(exc)],
            }
        )

    try:
        result = simulate_deck(
            deck,
            simulator=simulator,
            timeout_s=timeout_s,
        )
    except SpiceError as exc:
        return _text_result(
            {
                "ok": False,
                "stage": "simulation",
                "error": str(exc),
                "error_type": type(exc).__name__,
                "deck": deck.text,
            }
        )

    if not assertions:
        return _text_result(
            {"ok": True, "result": result.to_dict(max_points=max_points)}
        )

    report = check_all(result, assertions)
    payload = report.to_dict(max_points=max_points)
    payload["ok"] = True
    payload["summary"] = report.summary()
    return _text_result(payload)


def _tool_spice_capabilities(args: dict[str, Any]) -> dict[str, Any]:
    return _text_result(
        {
            "simulators": [sim.name for sim in available_simulators()],
            "measurement_kinds": sorted(MEASUREMENT_KINDS),
            "analysis_kinds": ["op", "tran", "ac", "dc"],
            "operators": ["<", "<=", ">", ">=", "==", "!=", "within"],
        }
    )


DISPATCH: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "validate_circuit": _tool_validate_circuit,
    "build_board": _tool_build_board,
    "emit_kicad_pcb": _tool_emit_kicad_pcb,
    "place_parts": _tool_place_parts,
    "generate_footprint": _tool_generate_footprint,
    "simulate_circuit": _tool_simulate_circuit,
    "spice_capabilities": _tool_spice_capabilities,
}


def _ok(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def handle(request: dict[str, Any]) -> dict[str, Any] | None:
    """Map one JSON-RPC request to one response.

    Returns ``None`` for notifications, which by protocol get no reply.
    """
    if request.get("jsonrpc") != "2.0":
        return _err(request.get("id"), INVALID_REQUEST, "jsonrpc must be '2.0'")

    method = request.get("method")
    req_id = request.get("id")
    params = request.get("params") or {}
    is_notification = "id" not in request

    if method == "initialize":
        return _ok(
            req_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            },
        )

    if method in ("notifications/initialized", "initialized"):
        return None

    if method == "ping":
        return _ok(req_id, {})

    if method == "tools/list":
        return _ok(req_id, {"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name")
        handler = DISPATCH.get(name)
        if handler is None:
            return _err(req_id, INVALID_PARAMS, f"unknown tool: {name!r}")
        try:
            return _ok(req_id, handler(params.get("arguments") or {}))
        except ValidationError as exc:
            # A malformed circuit is the caller's problem to fix, not a crash.
            return _ok(req_id, _error_result("; ".join(exc.errors)))
        except Exception as exc:
            return _ok(req_id, _error_result(f"{type(exc).__name__}: {exc}"))

    if is_notification:
        return None
    return _err(req_id, METHOD_NOT_FOUND, f"unknown method: {method!r}")


class Server:
    """Line-delimited JSON-RPC over a pair of streams."""

    def __init__(self, stdin=None, stdout=None):
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout

    def serve_forever(self) -> None:
        for line in self.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError as exc:
                self._write(_err(None, PARSE_ERROR, f"invalid JSON: {exc}"))
                continue
            if not isinstance(request, dict):
                self._write(_err(None, INVALID_REQUEST, "request must be an object"))
                continue
            response = handle(request)
            if response is not None:
                self._write(response)

    def _write(self, payload: dict[str, Any]) -> None:
        self.stdout.write(json.dumps(payload) + "\n")
        self.stdout.flush()


def main() -> int:
    Server().serve_forever()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from silkscreen.board import BoardResult, PlacedPart
from silkscreen.footprints import for_passive, sot223
from silkscreen.placement.adapter import (
    OUTLINE_MARGIN_MM,
    apply_verified_board,
    repair_generated_board,
    verifier_board,
)
from silkscreen.placement.agent import PlacementAgent, PlacementPolicyError
from silkscreen.placement.api import repair_request, run_to_dict
from silkscreen.placement.grader import (
    apply_model_output,
    board_from_dict,
    board_score,
    board_to_dict,
    board_to_json,
    is_legal,
    outcome_reward,
    progress_reward,
    quality_reward,
)
from silkscreen.placement.ollama_policy import OllamaPlacementModel
from silkscreen.placement.pcb_repair import (
    Board,
    CompanyProfile,
    Component,
    Keepout,
    apply_actions,
    demo_board,
    evaluate,
    get_profile,
    parse_actions,
    repair,
)
from silkscreen.placement.synthetic import corrupt, trajectory, write_jsonl
from silkscreen.placement.tinker_policy import TinkerPlacementModel
from silkscreen.placement.traces import JsonlFailureTraceStore, build_failure_traces
from silkscreen.units import mm, to_mm


@dataclass
class QueueModel:
    responses: list[str]
    calls: list[dict] = field(default_factory=list)

    def generate(self, prompt: str, **kwargs) -> str:
        self.calls.append({"prompt": prompt, **kwargs})
        return self.responses.pop(0)


def test_demo_board_is_corrupted_and_repair_is_legal() -> None:
    board = demo_board()
    profile = get_profile("compact-control")
    assert not is_legal(board, profile)

    repaired, actions = repair(board, profile)

    assert actions
    assert is_legal(repaired, profile)
    assert board_score(repaired, profile) < board_score(board, profile)
    assert repaired.component("J1") == board.component("J1")


def test_profiles_produce_distinct_legal_placements() -> None:
    board = demo_board()
    compact, _ = repair(board, get_profile("compact-control"))
    thermal, _ = repair(board, get_profile("thermal-first"))

    assert is_legal(compact, get_profile("compact-control"))
    assert is_legal(thermal, get_profile("thermal-first"))
    compact_xy = [(part.ref, part.x, part.y) for part in compact.components]
    thermal_xy = [(part.ref, part.x, part.y) for part in thermal.components]
    assert compact_xy != thermal_xy


def test_parser_ignores_garbage_unknown_refs_and_fixed_moves() -> None:
    board = demo_board()
    profile = get_profile("compact-control")
    actions = parse_actions(
        "thinking aloud\nMOVE NOPE 2 3\nPLACE J1 20 20\nPLACE C1 10.5 4 90"
    )
    moved = apply_actions(board, actions, profile)

    assert len(actions) == 3
    assert moved.component("J1") == board.component("J1")
    assert moved.component("C1").x == 10.5
    assert moved.component("C1").angle == 90


def test_keepout_and_boundary_are_hard_violations() -> None:
    profile = CompanyProfile("test", clearance=0.5, edge_margin=1.0)
    board = Board(
        20,
        10,
        (Component("U1", -1, 2, 4, 4), Component("C1", 12, 2, 3, 3)),
        (Keepout("boss", 11, 1, 5, 5),),
    )
    result = evaluate(board, profile)

    assert {violation.kind for violation in result.violations} == {
        "boundary",
        "keepout",
    }
    assert result.hard > 0


def test_rewards_prioritize_legality_and_track_progress() -> None:
    start = demo_board()
    profile = get_profile("compact-control")
    final, _ = repair(start, profile)

    assert outcome_reward(start, profile) == 0.0
    assert outcome_reward(final, profile) == 1.0
    assert progress_reward(start, final, profile) == 1.0
    assert 0 < quality_reward(start, final, profile) <= 0.1


def test_serialization_round_trip() -> None:
    board = demo_board()
    restored = board_from_dict(json.loads(board_to_json(board)))
    assert board_to_dict(restored) == board_to_dict(board)


def test_board_json_rejects_non_string_component_refs() -> None:
    board = board_to_dict(demo_board())
    board["components"][0]["ref"] = 1

    with pytest.raises(ValueError, match="component ref"):
        board_from_dict(board)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("fixed_refs", None),
        ("edge_refs", "J1"),
        ("groups", {"power": ["U1", "C1"]}),
        ("thermal_pairs", [["U1", "U2"]]),
    ],
)
def test_custom_profile_rejects_malformed_collections(
    field: str, value: object
) -> None:
    with pytest.raises(ValueError, match="profile"):
        repair_request({"profile": {"name": "custom", field: value}})


def test_action_parser_caps_model_output() -> None:
    text = "\n".join(f"PLACE C{index} 1 1" for index in range(100))

    assert len(parse_actions(text)) == 64


def canonical_board(*, overlap: bool = False) -> BoardResult:
    return BoardResult(
        parts=[
            PlacedPart(
                "U1",
                sot223(),
                value="AMS1117-3.3",
                x_nm=mm(2.0),
                y_nm=mm(2.0),
            ),
            PlacedPart(
                "C1",
                for_passive("capacitor", "10uF"),
                value="10uF",
                x_nm=mm(2.0 if overlap else 14.0),
                y_nm=mm(2.0 if overlap else 3.0),
                rotated=True,
            ),
        ],
        nets=["GND", "VIN", "VOUT"],
        width_nm=mm(32.0),
        height_nm=mm(22.0),
        solver_status="OPTIMAL",
    )


def test_canonical_board_adapter_preserves_refs_dimensions_and_rotation() -> None:
    source = canonical_board()
    projected = verifier_board(source)

    # The verifier frame is the emitted Edge.Cuts rectangle, which
    # board.emit_kicad_pcb draws OUTLINE_MARGIN_MM outside the packing
    # rectangle on every side -- see adapter.OUTLINE_MARGIN_MM.
    assert (projected.width, projected.height) == (
        32.0 + 2 * OUTLINE_MARGIN_MM,
        22.0 + 2 * OUTLINE_MARGIN_MM,
    )
    assert [component.ref for component in projected.components] == ["U1", "C1"]
    assert projected.component("U1").x == 2.0 + OUTLINE_MARGIN_MM
    assert projected.component("U1").width == to_mm(
        2 * source.parts[0].footprint.courtyard_w_nm
    )
    assert projected.component("C1").angle == 90


def test_verified_coordinates_write_back_before_copper_exists() -> None:
    source = canonical_board()
    source.wirelength_nm = mm(12.0)
    projected = verifier_board(source)
    moved = projected.replace_component(
        Component(
            **{
                **projected.component("U1").__dict__,
                "x": 5.25 + OUTLINE_MARGIN_MM,
                "y": 6.5 + OUTLINE_MARGIN_MM,
                "angle": 90,
            }
        )
    )

    updated = apply_verified_board(source, moved)

    assert updated.parts[0].x_nm == mm(5.25)
    assert updated.parts[0].y_nm == mm(6.5)
    assert updated.parts[0].rotated is True
    assert updated.nets == source.nets
    assert updated.wirelength_nm is None


def test_unchanged_verified_coordinates_preserve_solver_wirelength() -> None:
    source = canonical_board()
    source.wirelength_nm = mm(12.0)

    updated = apply_verified_board(source, verifier_board(source))

    assert updated.wirelength_nm == source.wirelength_nm


def test_verified_coordinates_refuse_to_rewrite_a_routed_board() -> None:
    source = canonical_board()
    source.tracks.append(object())

    with pytest.raises(ValueError, match="before copper routing"):
        apply_verified_board(source, verifier_board(source))


def test_generated_board_repair_is_applied_only_after_hard_legality() -> None:
    source = canonical_board(overlap=True)

    result = repair_generated_board(source, profile="compact-control")

    assert result.applied is True
    assert result.run.completed is True
    assert evaluate(result.run.board, result.run.profile).hard == 0
    assert result.board.parts != source.parts


def test_generated_board_repair_falls_back_from_unrepresentable_rotation() -> None:
    source = canonical_board(overlap=True)
    # Coordinates are in the verifier frame, which is the emitted outline.
    x, y = 14 + OUTLINE_MARGIN_MM, 3 + OUTLINE_MARGIN_MM
    model = QueueModel(responses=[f"PLACE C1 {x} {y} 180"])

    result = repair_generated_board(
        source,
        profile="compact-control",
        policy="gemini",
        model=model,
        max_turns=1,
    )

    assert result.applied is True
    assert result.run.policy == "deterministic"
    assert result.attempted_run is not None
    assert result.attempted_run.policy == "gemini"
    assert result.policy_fallback == {
        "from": "gemini",
        "to": "deterministic",
        "reason": "proposal backend returned an unsupported rotation",
    }
    assert all(component.angle in (0, 90) for component in result.run.board.components)


def test_generated_board_repair_retains_an_incomplete_policy_attempt() -> None:
    source = canonical_board(overlap=True)
    model = QueueModel(responses=["PLACE C1 2 2"])

    result = repair_generated_board(
        source,
        profile="compact-control",
        policy="gemini",
        model=model,
        max_turns=1,
    )

    assert result.applied is True
    assert result.run.policy == "deterministic"
    assert result.attempted_run is not None
    assert result.attempted_run.policy == "gemini"
    assert result.attempted_run.completed is False
    assert result.attempted_run.steps[0].proposed


def test_generated_board_repair_falls_back_from_invalid_model_action() -> None:
    source = canonical_board(overlap=True)
    model = QueueModel(responses=["PLACE C1 1001 3"])

    result = repair_generated_board(
        source,
        profile="compact-control",
        policy="gemini",
        model=model,
        max_turns=1,
    )

    assert result.applied is True
    assert result.run.policy == "deterministic"
    assert result.policy_fallback["reason"] == "proposal backend failed"


@pytest.mark.parametrize("value", [True, 1.5, "2"])
def test_repair_request_rejects_coerced_turn_budgets(value: object) -> None:
    with pytest.raises(ValueError, match="max_turns must be an integer"):
        repair_request({"max_turns": value})


@pytest.mark.parametrize(
    ("field", "value"),
    [("max_steps", 1.5), ("max_steps", True), ("preference_steps", "2")],
)
def test_repair_rejects_non_integer_step_budgets(field: str, value: object) -> None:
    with pytest.raises(ValueError, match="must be an integer"):
        repair(demo_board(), get_profile("compact-control"), **{field: value})


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_board_geometry_is_rejected(value: float) -> None:
    board = board_to_dict(demo_board())
    board["components"][0]["x"] = value

    with pytest.raises(ValueError, match="finite number"):
        board_from_dict(board)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_profile_values_are_rejected(value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        repair_request(
            {"profile": {"name": "unsafe", "clearance": value}}
        )


def test_non_finite_feedback_values_are_rejected() -> None:
    with pytest.raises(ValueError, match="finite"):
        repair_request(
            {
                "profile": "compact-control",
                "feedback": {"weights": {"thermal_weight": math.nan}},
            }
        )


def test_scripted_agent_accepts_improving_move_and_ignores_hallucination() -> None:
    profile = CompanyProfile("simple", clearance=0.5, edge_margin=0.5)
    board = Board(
        20,
        12,
        (Component("U1", 2, 2, 5, 5), Component("C1", 4, 3, 3, 3)),
    )
    model = QueueModel(responses=["PLACE C1 12 3\nMOVE MADE_UP 3 3"])

    run = PlacementAgent(model, max_turns=2).run(board, profile, policy="gemini")

    assert run.completed
    assert len(run.steps) == 1
    assert [action.ref for action in run.steps[0].proposed] == ["C1", "MADE_UP"]
    assert [action.ref for action in run.steps[0].accepted] == ["C1"]
    assert [receipt.accepted for receipt in run.steps[0].receipts] == [True, False]
    assert is_legal(run.board, profile)
    assert model.calls[0]["system"].startswith("You are a PCB placement")


def test_agent_rejects_a_worse_batch() -> None:
    profile = CompanyProfile("simple", clearance=0.5, edge_margin=0.5)
    board = Board(20, 12, (Component("U1", 2, 2, 4, 4),))
    model = QueueModel(responses=["PLACE U1 -4 -4"])

    run = PlacementAgent(model, max_turns=1).run(board, profile, policy="gemini")

    assert run.steps[0].accepted == ()
    assert run.board == board


def test_hybrid_policy_uses_gemini_only_after_fast_policy_stalls() -> None:
    profile = CompanyProfile("simple", clearance=0.5, edge_margin=0.5)
    board = Board(
        20,
        12,
        (Component("U1", 2, 2, 5, 5), Component("C1", 4, 3, 3, 3)),
    )
    fast = QueueModel(responses=["PLACE C1 3 3"])
    recovery = QueueModel(responses=["PLACE C1 12 3"])

    run = PlacementAgent(
        fast, fallback_model=recovery, max_turns=1
    ).run(board, profile, policy="hybrid")

    assert run.completed
    assert [step.proposer for step in run.steps] == ["tinker", "gemini-recovery"]
    assert run.steps[0].accepted == ()
    assert run.steps[1].accepted


def test_failed_policy_step_becomes_recovery_training_pair(tmp_path) -> None:
    profile = CompanyProfile("simple", clearance=0.5, edge_margin=0.5)
    board = Board(
        20,
        12,
        (Component("U1", 2, 2, 5, 5), Component("C1", 4, 3, 3, 3)),
    )
    fast = QueueModel(responses=["PLACE C1 3 3"])
    recovery = QueueModel(responses=["PLACE C1 12 3"])
    run = PlacementAgent(
        fast, fallback_model=recovery, max_turns=1
    ).run(board, profile, policy="hybrid")

    traces = build_failure_traces(
        run_to_dict(run),
        model_id="Qwen/Qwen3.5-4B",
        input_origin="demo-board",
        now=lambda: 123.0,
        id_factory=lambda: "trace-1",
    )

    assert len(traces) == 1
    trace = traces[0]
    assert trace["failure_kind"] == "all-actions-rejected"
    assert trace["chosen_source"] == "gemini-recovery"
    assert trace["rejected_response"] == "PLACE C1 3 3"
    assert trace["chosen_response"] == "PLACE C1 12 3"
    assert trace["post_training"] == {
        "prompt": trace["prompt"],
        "chosen": "PLACE C1 12 3",
        "rejected": "PLACE C1 3 3",
    }

    store = JsonlFailureTraceStore(tmp_path / "failures.jsonl")
    assert store.append(trace) == "trace-1"
    saved = json.loads((tmp_path / "failures.jsonl").read_text())
    assert saved["trace_id"] == "trace-1"


def test_turn_limit_failure_uses_deterministic_oracle_target() -> None:
    profile = CompanyProfile("simple", clearance=0.5, edge_margin=0.5)
    board = Board(
        20,
        12,
        (
            Component("U1", 2, 2, 5, 5),
            Component("C1", 4, 3, 3, 3),
            Component("C2", 5, 4, 3, 3),
        ),
    )
    model = QueueModel(responses=["PLACE C1 12 3"])
    run = PlacementAgent(model, max_turns=1).run(
        board, profile, policy="tinker"
    )
    assert not run.completed

    traces = build_failure_traces(
        run_to_dict(run),
        model_id="Qwen/Qwen3.5-4B",
        input_origin="demo-board",
        id_factory=lambda: "trace-2",
    )

    assert traces[0]["failure_kind"] == "turn-limit-illegal"
    assert traces[0]["chosen_source"] == "deterministic-oracle"
    assert traces[0]["chosen_response"].startswith(("PLACE ", "MOVE "))


def test_model_output_helper_uses_same_action_contract() -> None:
    profile = CompanyProfile("simple")
    board = Board(20, 12, (Component("U1", 2, 2, 4, 4),))
    moved = apply_model_output(board, "MOVE U1 2 -1", profile)
    assert (moved.component("U1").x, moved.component("U1").y) == (4, 1)


def test_synthetic_trajectory_is_deterministic_and_repairable() -> None:
    first = trajectory(7)
    second = trajectory(7)
    assert first == second
    assert first["completion"]
    assert [message["role"] for message in first["messages"]] == [
        "system",
        "user",
        "assistant",
    ]
    assert corrupt(demo_board(), 7) == corrupt(demo_board(), 7)


def test_synthetic_export_creates_its_destination_directory(tmp_path) -> None:
    output = write_jsonl(tmp_path / "nested" / "placements.jsonl", [0, 1])

    assert output.is_file()
    assert len(output.read_text(encoding="utf-8").splitlines()) == 2


def test_tinker_adapter_samples_checkpoint_with_chat_rendering() -> None:
    calls: list[dict] = []

    class Tokenizer:
        def apply_chat_template(self, messages, **kwargs):
            calls.append({"messages": messages, "template": kwargs})
            return "rendered"

        def encode(self, text):
            assert text == "rendered"
            return [1, 2, 3]

        def decode(self, tokens, **kwargs):
            assert tokens == [8, 9]
            return "PLACE C1 12 3"

    class Future:
        def result(self):
            return SimpleNamespace(
                sequences=[SimpleNamespace(tokens=[8, 9])]
            )

    class Sampling:
        def get_tokenizer(self):
            return Tokenizer()

        def sample(self, **kwargs):
            calls.append({"sample": kwargs})
            return Future()

    class Service:
        def create_sampling_client(self, **kwargs):
            calls.append({"client": kwargs})
            return Sampling()

    class ModelInput:
        @staticmethod
        def from_ints(tokens):
            return tuple(tokens)

    class SamplingParams:
        def __init__(self, **kwargs):
            self.values = kwargs

    fake_tinker = SimpleNamespace(
        types=SimpleNamespace(
            ModelInput=ModelInput,
            SamplingParams=SamplingParams,
        )
    )
    model = TinkerPlacementModel(
        model_path="tinker://run/sampler_weights/final",
        service_client=Service(),
        tinker_module=fake_tinker,
    )

    output = model.generate("board", system="rules", max_output_tokens=64)

    assert output == "PLACE C1 12 3"
    assert calls[0]["client"]["model_path"].startswith("tinker://")
    assert calls[-1]["sample"]["num_samples"] == 1


def test_ollama_adapter_uses_private_chat_endpoint() -> None:
    calls: list[dict] = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return json.dumps(
                {"message": {"content": "PLACE C1 12 3"}}
            ).encode()

    def open_request(request, *, timeout):
        calls.append(
            {
                "url": request.full_url,
                "payload": json.loads(request.data),
                "timeout": timeout,
            }
        )
        return Response()

    model = OllamaPlacementModel(
        base_url="http://127.0.0.1:11435",
        model="gemma3:4b",
        opener=open_request,
    )
    output = model.generate("board", system="rules", max_output_tokens=64)

    assert output == "PLACE C1 12 3"
    assert calls[0]["url"].endswith("/api/chat")
    assert calls[0]["payload"]["model"] == "gemma3:4b"
    assert calls[0]["payload"]["stream"] is False


@pytest.mark.parametrize(
    "payload",
    [b"{not-json", b'{"message": {}}', b'{"message": {"content": 7}}'],
)
def test_ollama_adapter_normalizes_malformed_responses(payload: bytes) -> None:
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return payload

    model = OllamaPlacementModel(
        base_url="http://127.0.0.1:11435",
        opener=lambda *args, **kwargs: Response(),
    )

    with pytest.raises(PlacementPolicyError):
        model.generate("board")


@pytest.mark.parametrize("name", ["", "unknown", "Thermalish"])
def test_unknown_profile_is_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="unknown placement profile"):
        get_profile(name)

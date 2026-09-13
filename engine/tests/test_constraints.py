import pytest
from silkscreen.board import BoardResult, PlacedPart
from silkscreen.constraints import (
    ConstraintManifest,
    parse_constraint_manifest,
    verify_constraint_manifest,
)
from silkscreen.footprints import Footprint
from silkscreen.netlist import CircuitSpec, Connection, Device, Passive, PassiveType
from silkscreen.packing import Layer
from silkscreen.routing import RouteResult, Track, Via
from silkscreen.units import mm


def _i2c_manifest(**updates):
    net_class = {
        "name": "I2C bus",
        "kind": "i2c",
        "nets": ["SDA", "SCL"],
        "allowed_layers": ["F.Cu"],
        "max_layer_transitions": 0,
        "max_vias_per_net": 0,
        "signal_voltage_v": 3.3,
        "max_frequency_hz": 400_000,
        "pullups_required": True,
        "pullup_rail": "3V3",
        "pullup_min_ohms": 1_000,
        "pullup_max_ohms": 10_000,
        "bus_capacitance_pf": 50,
        "max_rise_time_ns": 300,
        "min_trace_width_mm": 0.2,
        "min_thermal_separation_mm": None,
        "max_length_mm": 20,
        "max_skew_mm": 1,
        "concerns": ["Pull-ups", "rise time"],
    }
    net_class.update(updates)
    return {
        "version": 2,
        "approved": True,
        "board_layers": 2,
        "net_classes": [net_class],
        "mechanical": {"max_board_width_mm": 20, "max_board_height_mm": 20},
        "soft_preferences": {"fewer_vias": 1, "shorter_traces": 0.1},
    }


def _i2c_spec(*, include_scl_pullup=True):
    passives = [Passive("R_SDA", PassiveType.RESISTOR, "4.7k")]
    connections = [
        Connection("SDA", ("MCU.SDA", "R_SDA.1")),
        Connection("3V3", ("MCU.VDD", "R_SDA.2")),
        Connection("SCL", ("MCU.SCL", "SENSOR.SCL")),
    ]
    devices = [
        Device("MCU", {"SDA": "1", "SCL": "2", "VDD": "3"}),
        Device("SENSOR", {"SCL": "1"}),
    ]
    if include_scl_pullup:
        passives.append(Passive("R_SCL", PassiveType.RESISTOR, "4.7k"))
        connections[1] = Connection("3V3", ("MCU.VDD", "R_SDA.2", "R_SCL.2"))
        connections[2] = Connection("SCL", ("MCU.SCL", "SENSOR.SCL", "R_SCL.1"))
    spec = CircuitSpec(devices=devices, passives=passives, connections=connections)
    spec.validate()
    return spec


def _board():
    return BoardResult(
        parts=[],
        nets=["SDA", "SCL", "3V3"],
        width_nm=mm(10),
        height_nm=mm(8),
        solver_status="OPTIMAL",
    )


def _part(ref, value, x_mm, y_mm):
    footprint = Footprint(
        name="test",
        courtyard_w_nm=mm(0.5),
        courtyard_h_nm=mm(0.5),
    )
    return PlacedPart(
        ref=ref,
        value=value,
        footprint=footprint,
        x_nm=mm(x_mm),
        y_nm=mm(y_mm),
    )


def _route(*, width_mm=0.2, sda_mm=8, scl_mm=8, with_via=False):
    tracks = [
        Track(0, 0, mm(sda_mm), 0, Layer.TOP, "SDA", mm(width_mm)),
        Track(0, mm(1), mm(scl_mm), mm(1), Layer.TOP, "SCL", mm(width_mm)),
    ]
    vias = [Via(mm(1), 0, "SDA", mm(0.6), mm(0.3))] if with_via else []
    return RouteResult(tracks=tracks, vias=vias, routed=["SDA", "SCL"])


def _check(receipt, name):
    return next(
        check
        for group in receipt["net_classes"]
        for check in group["checks"]
        if check["name"] == name
    )


def test_optional_constraint_manifest_preserves_old_requests():
    assert parse_constraint_manifest(None) is None


def test_version_one_manifest_remains_parseable_but_cannot_fake_new_checks():
    raw = _i2c_manifest()
    raw["version"] = 1
    for field_name in (
        "pullup_rail",
        "pullup_min_ohms",
        "pullup_max_ohms",
        "bus_capacitance_pf",
        "max_rise_time_ns",
    ):
        raw["net_classes"][0].pop(field_name)

    manifest = ConstraintManifest.from_dict(raw)
    receipt = verify_constraint_manifest(manifest, _i2c_spec(), _board(), _route())

    assert receipt["hard_gate"] == "blocked"
    assert _check(receipt, "pullups")["status"] == "unresolved"


def test_manifest_round_trips_and_enters_the_agent_prompt():
    manifest = ConstraintManifest.from_dict(_i2c_manifest())

    assert manifest.to_dict()["net_classes"][0]["nets"] == ["SDA", "SCL"]
    assert "APPROVED PCB CONSTRAINT MANIFEST" in manifest.prompt_block()
    assert "Soft preferences are advisory scoring terms" in manifest.prompt_block()
    assert "never override a hard check" in manifest.prompt_block()


def test_unapproved_manifest_is_rejected_before_build():
    raw = _i2c_manifest()
    raw["approved"] = False
    with pytest.raises(ValueError, match="approved"):
        ConstraintManifest.from_dict(raw)


def test_kind_specific_required_values_cannot_be_omitted():
    raw = _i2c_manifest()
    raw["net_classes"][0]["bus_capacitance_pf"] = None

    with pytest.raises(ValueError, match="bus_capacitance_pf"):
        ConstraintManifest.from_dict(raw)


def test_i2c_pullups_and_rise_time_are_deterministically_verified():
    manifest = ConstraintManifest.from_dict(_i2c_manifest())

    receipt = verify_constraint_manifest(manifest, _i2c_spec(), _board(), _route())

    assert receipt["hard_gate"] == "blocked"
    assert _check(receipt, "signal_voltage")["status"] == "unresolved"
    pullups = _check(receipt, "pullups")
    assert pullups["status"] == "verified"
    assert pullups["evidence"]["resistance_ohms"] == {"SDA": 4700, "SCL": 4700}
    assert pullups["evidence"]["rise_time_ns"]["SDA"] == pytest.approx(199.1155)


def test_missing_i2c_pullup_blocks_promotion():
    manifest = ConstraintManifest.from_dict(_i2c_manifest())

    receipt = verify_constraint_manifest(
        manifest, _i2c_spec(include_scl_pullup=False), _board(), _route()
    )

    assert receipt["promotable"] is False
    assert _check(receipt, "pullups")["status"] == "violated"
    assert _check(receipt, "pullups")["evidence"]["missing"] == ["SCL"]


@pytest.mark.parametrize(
    ("route", "check_name"),
    [
        (_route(with_via=True), "vias_and_layer_transitions"),
        (_route(width_mm=0.15), "trace_width"),
        (_route(sda_mm=21), "trace_length"),
        (_route(sda_mm=10, scl_mm=8), "skew"),
    ],
)
def test_routed_geometry_violations_block_promotion(route, check_name):
    manifest = ConstraintManifest.from_dict(_i2c_manifest())

    receipt = verify_constraint_manifest(manifest, _i2c_spec(), _board(), route)

    assert receipt["promotable"] is False
    assert _check(receipt, check_name)["status"] == "violated"


def test_controlled_impedance_stays_unresolved_without_a_field_solver():
    raw = _i2c_manifest(
        kind="usb",
        name="USB",
        nets=["SDA", "SCL"],
        pullups_required=False,
        controlled_impedance=True,
        impedance_ohms=90,
        impedance_tolerance_percent=10,
        pair_spacing_mm=0.2,
        reference_plane="GND",
    )
    manifest = ConstraintManifest.from_dict(raw)

    receipt = verify_constraint_manifest(manifest, _i2c_spec(), _board(), _route())

    assert receipt["hard_gate"] == "blocked"
    assert _check(receipt, "controlled_impedance")["status"] == "unresolved"


def test_power_voltage_drop_is_a_conservative_unresolved_estimate():
    raw = _i2c_manifest(
        kind="power",
        name="Power",
        pullups_required=False,
        expected_current_a=10,
        copper_weight_oz=1,
        max_voltage_drop_v=0.01,
        min_trace_width_mm=0.1,
        min_thermal_separation_mm=5,
    )
    manifest = ConstraintManifest.from_dict(raw)

    receipt = verify_constraint_manifest(
        manifest,
        _i2c_spec(),
        _board(),
        _route(width_mm=0.1, sda_mm=20, scl_mm=20),
    )

    drop = _check(receipt, "voltage_drop")
    assert drop["status"] == "unresolved"
    assert drop["evidence"]["conservative_lower_bound_v"]["SDA"] == 0
    assert drop["evidence"]["copper_only_estimate_v"]["SDA"] > 0.01


def test_thermal_separation_is_measured_between_approved_reference_pairs():
    raw = _i2c_manifest(
        min_thermal_separation_mm=5, thermal_pairs=[["U1", "U2"]]
    )
    manifest = ConstraintManifest.from_dict(raw)
    board = _board()
    board.parts = [
        _part("U1", "motor driver", 0, 0),
        _part("U2", "regulator", 2, 0),
    ]

    receipt = verify_constraint_manifest(manifest, _i2c_spec(), board, _route())

    thermal = _check(receipt, "thermal_separation")
    assert thermal["status"] == "violated"
    assert thermal["evidence"]["measured_mm"]["U1/U2"] == pytest.approx(2)


def test_mechanical_height_and_missing_mounting_hole_block_promotion():
    raw = _i2c_manifest()
    raw["mechanical"].update(
        max_component_height_mm=4,
        mounting_hole_refs=["H1"],
    )
    manifest = ConstraintManifest.from_dict(raw)

    receipt = verify_constraint_manifest(manifest, _i2c_spec(), _board(), _route())

    statuses = {check["name"]: check["status"] for check in receipt["mechanical"]}
    assert statuses["board_outline"] == "verified"
    assert statuses["mounting_holes"] == "violated"
    assert statuses["component_height"] == "unresolved"


def test_keepout_and_fixed_placement_are_checked_against_real_footprints():
    raw = _i2c_manifest()
    raw["mechanical"].update(
        keepouts=[
            {"name": "antenna", "x_mm": 0, "y_mm": 0, "width_mm": 2, "height_mm": 2}
        ],
        fixed_placements=[{"ref": "J1", "x_mm": 4, "y_mm": 4, "tolerance_mm": 0.1}],
    )
    manifest = ConstraintManifest.from_dict(raw)
    board = _board()
    board.parts = [_part("J1", "USB connector", 0, 0)]

    receipt = verify_constraint_manifest(manifest, _i2c_spec(), board, _route())

    statuses = {check["name"]: check for check in receipt["mechanical"]}
    assert statuses["mechanical_keepouts"]["status"] == "violated"
    assert statuses["mechanical_keepouts"]["evidence"]["collisions"] == {
        "antenna": ["J1"]
    }
    assert statuses["fixed_placements"]["status"] == "violated"


def test_soft_preferences_produce_cost_but_never_change_the_hard_gate():
    manifest = ConstraintManifest.from_dict(_i2c_manifest())

    receipt = verify_constraint_manifest(manifest, _i2c_spec(), _board(), _route())

    assert receipt["hard_gate"] == "blocked"
    assert receipt["soft_preferences"]["cost"] > 0


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("kind", "mystery", "kind"),
        ("allowed_layers", ["In1.Cu"], "layer"),
        ("min_trace_width_mm", 0, "supported range"),
    ],
)
def test_v2_rejects_invalid_schema_values(field, value, message):
    raw = _i2c_manifest()
    raw["net_classes"][0][field] = value
    with pytest.raises(ValueError, match=message):
        ConstraintManifest.from_dict(raw)


def test_v2_rejects_unknown_fields_and_degenerate_keepouts():
    raw = _i2c_manifest()
    raw["net_classes"][0]["surprise"] = True
    with pytest.raises(ValueError, match="unknown"):
        ConstraintManifest.from_dict(raw)

    raw = _i2c_manifest()
    raw["mechanical"]["keepouts"] = [
        {"name": "bad", "x_mm": 0, "y_mm": 0, "width_mm": 0, "height_mm": 1}
    ]
    with pytest.raises(ValueError, match="supported range"):
        ConstraintManifest.from_dict(raw)


def test_two_layer_schema_and_actual_board_model_are_fail_closed():
    raw = _i2c_manifest()
    raw["board_layers"] = 4
    with pytest.raises(ValueError, match="two copper layers"):
        ConstraintManifest.from_dict(raw)

    raw["version"] = 1
    manifest = ConstraintManifest.from_dict(raw)
    receipt = verify_constraint_manifest(manifest, _i2c_spec(), _board(), _route())
    mechanical = {check["name"]: check for check in receipt["mechanical"]}
    assert mechanical["board_layers"]["status"] == "violated"


def test_missing_route_geometry_leaves_dependent_checks_unresolved():
    manifest = ConstraintManifest.from_dict(_i2c_manifest())
    route = RouteResult(tracks=_route().tracks[:1], vias=[], routed=["SDA", "SCL"])
    receipt = verify_constraint_manifest(manifest, _i2c_spec(), _board(), route)

    for name in (
        "routing",
        "allowed_layers",
        "vias_and_layer_transitions",
        "trace_width",
        "trace_length",
        "skew",
    ):
        assert _check(receipt, name)["status"] == "unresolved"


def test_parallel_pullups_use_all_matching_resistors():
    manifest = ConstraintManifest.from_dict(_i2c_manifest())
    spec = _i2c_spec()
    spec.passives.extend(
        [
            Passive("R_SDA_2", PassiveType.RESISTOR, "4.7k"),
            Passive("R_SCL_2", PassiveType.RESISTOR, "4.7k"),
        ]
    )
    spec.connections = [
        Connection("SDA", ("MCU.SDA", "R_SDA.1", "R_SDA_2.1")),
        Connection(
            "3V3", ("MCU.VDD", "R_SDA.2", "R_SCL.2", "R_SDA_2.2", "R_SCL_2.2")
        ),
        Connection("SCL", ("MCU.SCL", "SENSOR.SCL", "R_SCL.1", "R_SCL_2.1")),
    ]
    spec.validate()

    pullups = _check(
        verify_constraint_manifest(manifest, spec, _board(), _route()), "pullups"
    )
    assert pullups["evidence"]["resistance_ohms"] == {"SDA": 2350, "SCL": 2350}
    assert [item["ref"] for item in pullups["evidence"]["resistors"]["SDA"]] == [
        "R_SDA",
        "R_SDA_2",
    ]


def test_thermal_and_mounting_claims_need_explicit_physical_evidence():
    raw = _i2c_manifest(min_thermal_separation_mm=5)
    raw["mechanical"]["mounting_hole_refs"] = ["H1"]
    manifest = ConstraintManifest.from_dict(raw)
    board = _board()
    board.parts = [_part("U1", "regulator", 0, 0), _part("H1", "hole", 2, 0)]

    receipt = verify_constraint_manifest(manifest, _i2c_spec(), board, _route())
    assert _check(receipt, "thermal_separation")["status"] == "unresolved"
    holes = next(
        check
        for check in receipt["mechanical"]
        if check["name"] == "mounting_holes"
    )
    assert holes["status"] == "unresolved"
    assert holes["evidence"]["without_drill_evidence"] == ["H1"]


def test_duplicate_net_ownership_and_board_refs_are_rejected():
    raw = _i2c_manifest()
    raw["net_classes"].append({**raw["net_classes"][0], "name": "Other"})
    with pytest.raises(ValueError, match="belongs to both"):
        ConstraintManifest.from_dict(raw)

    manifest = ConstraintManifest.from_dict(_i2c_manifest())
    board = _board()
    board.parts = [_part("U1", "a", 0, 0), _part("U1", "b", 2, 0)]
    with pytest.raises(ValueError, match="board part refs"):
        verify_constraint_manifest(manifest, _i2c_spec(), board, _route())

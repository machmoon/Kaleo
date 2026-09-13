"""Tests for the enclosure IR (workstream A).

Discipline mirrors ``netlist.py``'s tests: every rejection case asserts the
specific message so a repair prompt actually names the problem, and the batch
property — one exception carrying *all* the errors — is asserted directly.
"""

import json

import pytest
from silkscreen.enclosure import (
    DEFAULT_CLEARANCE_NM,
    DEFAULT_WALL_NM,
    FACES,
    LIDS,
    MIN_WALL_NM,
    Cutout,
    EnclosureError,
    EnclosureSpec,
    EnclosureValidationError,
    parse_enclosure_spec,
)
from silkscreen.units import mm

GOOD = {
    "wall_mm": 2.0,
    "clearance_mm": 1.0,
    "lid": "lip",
    "corner_radius_mm": 2.5,
    "cutouts": [
        {"id": "usb", "ref": "J1", "face": "left", "margin_mm": 0.5},
    ],
    "standoffs": True,
    "vents": False,
    "label": "silkscreen",
}


def parse(overrides=None, **extra):
    data = {**GOOD, **(overrides or {}), **extra}
    return parse_enclosure_spec(json.dumps(data))


def errors_of(data) -> list[str]:
    with pytest.raises(EnclosureValidationError) as exc_info:
        parse_enclosure_spec(json.dumps(data))
    return exc_info.value.errors


# ---------------------------------------------------------------- happy path


def test_good_spec_parses_to_integer_nanometres():
    spec = parse()
    assert isinstance(spec, EnclosureSpec)
    assert spec.wall_nm == mm(2.0) == 2_000_000
    assert spec.clearance_nm == mm(1.0)
    assert spec.corner_radius_nm == mm(2.5)
    assert spec.lid == "lip"
    assert spec.standoffs is True
    assert spec.vents is False
    assert spec.label == "silkscreen"
    assert spec.cutouts == (
        Cutout(id="usb", ref="J1", face="left", margin_nm=mm(0.5)),
    )
    for value in (spec.wall_nm, spec.clearance_nm, spec.corner_radius_nm,
                  spec.cutouts[0].margin_nm):
        assert isinstance(value, int)


def test_spec_dataclasses_are_frozen():
    spec = parse()
    with pytest.raises(AttributeError):
        spec.wall_nm = 0
    with pytest.raises(AttributeError):
        spec.cutouts[0].ref = "J9"


def test_defaults_fill_missing_fields():
    spec = parse_enclosure_spec("{}")
    assert spec.wall_nm == DEFAULT_WALL_NM
    assert spec.clearance_nm == DEFAULT_CLEARANCE_NM
    assert spec.corner_radius_nm == 0
    assert spec.lid == "lip"
    assert spec.cutouts == ()
    assert spec.standoffs is True
    assert spec.vents is False
    assert spec.label is None


def test_fenced_json_is_tolerated():
    fenced = "```json\n" + json.dumps(GOOD) + "\n```"
    assert parse_enclosure_spec(fenced) == parse()


def test_already_decoded_dict_is_accepted():
    assert parse_enclosure_spec(dict(GOOD)) == parse()


def test_min_wall_constant_is_printable_fdm_minimum():
    assert mm(1.2) == MIN_WALL_NM
    assert mm(2.0) == DEFAULT_WALL_NM
    assert mm(1.0) == DEFAULT_CLEARANCE_NM
    assert FACES == ("left", "right", "front", "back", "top")
    assert LIDS == ("lip", "screw", "snap", "none")


# ----------------------------------------------------------- rejection cases


def test_not_json_raises_validation_error():
    with pytest.raises(EnclosureValidationError) as exc_info:
        parse_enclosure_spec("I would suggest a nice rounded case.")
    assert "not valid JSON" in exc_info.value.errors[0]


def test_json_array_is_rejected():
    with pytest.raises(EnclosureValidationError) as exc_info:
        parse_enclosure_spec("[1, 2]")
    assert "expected a JSON object" in exc_info.value.errors[0]


def test_zero_wall_is_rejected():
    errs = errors_of({**GOOD, "wall_mm": 0})
    assert any("'wall_mm'" in e and "positive" in e for e in errs)


def test_negative_wall_is_rejected():
    errs = errors_of({**GOOD, "wall_mm": -2.0})
    assert any("'wall_mm'" in e for e in errs)


def test_thin_wall_names_the_printable_minimum():
    errs = errors_of({**GOOD, "wall_mm": 0.8})
    assert any("1.2 mm" in e for e in errs)


def test_zero_clearance_is_rejected():
    errs = errors_of({**GOOD, "clearance_mm": 0})
    assert any("'clearance_mm'" in e for e in errs)


def test_negative_corner_radius_is_rejected():
    errs = errors_of({**GOOD, "corner_radius_mm": -1.0})
    assert any("'corner_radius_mm'" in e for e in errs)


def test_corner_radius_that_thins_the_corner_wall_is_batched():
    # Inline geometry: a rounded shell over a square cavity is thinnest on
    # the corner diagonal, sqrt(2)*w - r*(sqrt(2)-1). With w = 2 mm and the
    # 1.2 mm printable minimum that reaches the limit at r = 3.931 mm.
    import math

    w, minimum = 2.0, MIN_WALL_NM / 1_000_000
    r_max = (math.sqrt(2) * w - minimum) / (math.sqrt(2) - 1)
    assert parse(corner_radius_mm=round(r_max - 0.005, 3)).corner_radius_nm > 0
    errs = errors_of({**GOOD, "corner_radius_mm": round(r_max + 0.005, 3)})
    assert any("'corner_radius_mm'" in e and "corner" in e for e in errs)
    # Batched with everything else wrong in the same spec, not raised alone.
    errs = errors_of({**GOOD, "corner_radius_mm": 8.0, "lid": "hinge"})
    assert any("'corner_radius_mm'" in e for e in errs)
    assert any("'lid'" in e for e in errs)


def test_non_numeric_dimension_is_rejected():
    errs = errors_of({**GOOD, "wall_mm": "thick"})
    assert any("'wall_mm'" in e and "number" in e for e in errs)


def test_boolean_dimension_is_rejected():
    # bool is an int subclass; True must not slip through as 1 mm.
    errs = errors_of({**GOOD, "wall_mm": True})
    assert any("'wall_mm'" in e for e in errs)


def test_unknown_lid_is_rejected():
    errs = errors_of({**GOOD, "lid": "hinged"})
    assert any("'hinged'" in e and "lip" in e for e in errs)


def test_unknown_face_is_rejected():
    bad = {**GOOD, "cutouts": [{"id": "a", "ref": "J1", "face": "bottom"}]}
    errs = errors_of(bad)
    assert any("'bottom'" in e for e in errs)


def test_duplicate_cutout_ids_are_rejected():
    bad = {
        **GOOD,
        "cutouts": [
            {"id": "usb", "ref": "J1", "face": "left"},
            {"id": "usb", "ref": "J2", "face": "right"},
        ],
    }
    errs = errors_of(bad)
    assert any("duplicate cutout id 'usb'" in e for e in errs)


def test_malformed_ref_is_rejected():
    for ref in ("", "1J", "J1.pad", "J 1"):
        bad = {**GOOD, "cutouts": [{"id": "a", "ref": ref, "face": "left"}]}
        errs = errors_of(bad)
        assert any("reference designator" in e for e in errs), ref


def test_negative_cutout_margin_is_rejected():
    bad = {**GOOD,
           "cutouts": [{"id": "a", "ref": "J1", "face": "left",
                        "margin_mm": -0.5}]}
    errs = errors_of(bad)
    assert any("margin" in e for e in errs)


def test_non_object_cutout_is_rejected():
    errs = errors_of({**GOOD, "cutouts": ["J1"]})
    assert any("cutout[0]" in e for e in errs)


def test_cutouts_must_be_a_list():
    errs = errors_of({**GOOD, "cutouts": {"id": "a"}})
    assert any("'cutouts' must be a list" in e for e in errs)


def test_screw_lid_without_standoffs_is_rejected():
    # Pilot holes over an empty floor: the screws would bite nothing.
    errs = errors_of({**GOOD, "lid": "screw", "standoffs": False})
    assert any("'lid' is 'screw'" in e and "standoffs" in e for e in errs)


def test_screw_lid_with_standoffs_parses():
    spec = parse({"lid": "screw", "standoffs": True})
    assert spec.lid == "screw" and spec.standoffs is True
    # standoffs defaults to True, so omitting it is also legal.
    spec = parse_enclosure_spec(json.dumps({"lid": "screw"}))
    assert spec.standoffs is True


def test_screw_standoff_coupling_joins_the_batch():
    # The coupling error rides the same batched exception as other failures.
    errs = errors_of({
        **GOOD, "lid": "screw", "standoffs": False, "wall_mm": -1.0,
    })
    assert any("'wall_mm'" in e for e in errs)
    assert any("standoffs" in e for e in errs)


def test_non_boolean_flags_are_rejected():
    errs = errors_of({**GOOD, "standoffs": "yes", "vents": 1})
    assert any("'standoffs'" in e for e in errs)
    assert any("'vents'" in e for e in errs)


def test_unknown_field_is_rejected_not_silently_dropped():
    # "wall" instead of "wall_mm" must not silently become the default wall.
    errs = errors_of({**GOOD, "wall": 5.0})
    assert any("unknown field 'wall'" in e for e in errs)


def test_non_string_label_is_rejected():
    errs = errors_of({**GOOD, "label": 42})
    assert any("'label'" in e for e in errs)


def test_label_is_sanitised_not_rejected():
    spec = parse({"label": "  my <script> board!  v2  "})
    assert spec.label == "my script board v2"
    assert parse({"label": "<<<>>>"}).label is None
    long = parse({"label": "x" * 100})
    assert len(long.label) == 32


# ------------------------------------------------------------ batch property


def test_multiple_errors_reported_in_one_exception():
    bad = {
        "wall_mm": -1.0,                       # negative dim
        "clearance_mm": 0,                     # zero dim
        "lid": "hinged",                       # unknown lid
        "cutouts": [
            {"id": "a", "ref": "J1", "face": "nowhere"},   # unknown face
            {"id": "a", "ref": "??", "face": "left"},      # dup id + bad ref
        ],
        "standoffs": "yes",                    # wrong type
    }
    with pytest.raises(EnclosureValidationError) as exc_info:
        parse_enclosure_spec(json.dumps(bad))
    errs = exc_info.value.errors
    assert len(errs) >= 6
    joined = "\n".join(errs)
    assert "'wall_mm'" in joined
    assert "'clearance_mm'" in joined
    assert "'hinged'" in joined
    assert "'nowhere'" in joined
    assert "duplicate cutout id" in joined
    assert "reference designator" in joined
    assert "'standoffs'" in joined
    # The str() form carries every message — it *is* the repair prompt body.
    assert str(exc_info.value).count("- ") >= len(errs)


def test_validation_error_is_an_enclosure_error():
    assert issubclass(EnclosureValidationError, EnclosureError)

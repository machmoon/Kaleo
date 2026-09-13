"""Tests for :mod:`silkscreen.enclosure.board_shape` and ``.heights``.

Oracle discipline (the ``test_kicad.py`` convention): every expected extent is
computed **inline from the raw fixture's own literals** -- anchor plus
courtyard corner, rotation applied by hand -- never by calling the extractor
and comparing it to itself. The fixture is the same 11-footprint STM32 board
the rest of the suite uses; it ships without an ``Edge.Cuts`` outline, so the
outline cases append known ``gr_line`` rectangles to the raw text.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from silkscreen.enclosure.board_shape import (
    DEFAULT_BOARD_THICKNESS_NM,
    board_envelope,
    find_part,
)
from silkscreen.enclosure.heights import DEFAULT_HEIGHT_NM, HEIGHTS_NM, height_for
from silkscreen.packing import Layer
from silkscreen.units import mm

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ref.kicad_pcb"

pytestmark = pytest.mark.skipif(
    not FIXTURE.exists(), reason="board fixture not present"
)


def _nm(value_mm: float) -> int:
    """Raw mm literal -> nm, independent of the code under test."""
    return int(round(value_mm * 1_000_000))


# The outline appended to the fixture for envelope tests: a rectangle from
# (-4, -7) to (15, 19) in KiCad mm, drawn as four gr_lines the same way
# set_board_outline draws one.
OUTLINE_X0, OUTLINE_Y0, OUTLINE_X1, OUTLINE_Y1 = -4.0, -7.0, 15.0, 19.0


def _outlined_fixture(tmp_path: Path) -> Path:
    text = FIXTURE.read_text(encoding="utf-8")
    corners = [
        (OUTLINE_X0, OUTLINE_Y0),
        (OUTLINE_X1, OUTLINE_Y0),
        (OUTLINE_X1, OUTLINE_Y1),
        (OUTLINE_X0, OUTLINE_Y1),
    ]
    lines = []
    for i in range(4):
        sx, sy = corners[i]
        ex, ey = corners[(i + 1) % 4]
        lines.append(
            f'  (gr_line (start {sx} {sy}) (end {ex} {ey}) '
            f'(stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))'
        )
    body = text.rstrip()
    assert body.endswith(")")
    out = tmp_path / "outlined.kicad_pcb"
    out.write_text(body[:-1] + "\n" + "\n".join(lines) + "\n)\n", encoding="utf-8")
    return out


@pytest.fixture()
def envelope(tmp_path):
    return board_envelope(_outlined_fixture(tmp_path))


def _synthetic_board(tmp_path: Path, *, angle: float = 90.0,
                     footprint: str = "Foo:Bar_Widget",
                     layer: str = "F.Cu") -> Path:
    """One-footprint board with an asymmetric fp_rect courtyard, rotated."""
    at = f"(at 10 20 {angle:g})" if angle else "(at 10 20)"
    stroke = "(stroke (width 0.05) (type solid))"
    text = f"""(kicad_pcb (version 20240108) (generator "pcbnew")
  (generator_version "8.0")
  (general (thickness 1.2))
  (layers
    (0 "F.Cu" signal)
    (2 "B.Cu" signal)
    (44 "Edge.Cuts" user)
    (47 "F.CrtYd" user "F.Courtyard")
  )
  (net 0 "")
  (footprint "{footprint}" (layer "{layer}")
    {at}
    (property "Reference" "X1" (at 0 0 0) (layer "F.SilkS")
      (effects (font (size 1 1) (thickness 0.15))))
    (fp_rect (start -1 -0.5) (end 3 0.5)
      (stroke (width 0.05) (type solid)) (layer "F.CrtYd"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))
  )
  (gr_line (start 0 0) (end 30 0) {stroke} (layer "Edge.Cuts"))
  (gr_line (start 30 0) (end 30 40) {stroke} (layer "Edge.Cuts"))
  (gr_line (start 30 40) (end 0 40) {stroke} (layer "Edge.Cuts"))
  (gr_line (start 0 40) (end 0 0) {stroke} (layer "Edge.Cuts"))
)
"""
    out = tmp_path / "synthetic.kicad_pcb"
    out.write_text(text, encoding="utf-8")
    return out


# ---------------------------------------------------------------- outline


def test_board_without_outline_is_refused():
    with pytest.raises(ValueError, match="Edge.Cuts"):
        board_envelope(FIXTURE)


def test_outline_bbox_matches_the_appended_rectangle(envelope):
    assert envelope.x_min_nm == _nm(OUTLINE_X0)
    assert envelope.y_min_nm == _nm(OUTLINE_Y0)
    assert envelope.x_max_nm == _nm(OUTLINE_X1)
    assert envelope.y_max_nm == _nm(OUTLINE_Y1)


def test_outline_polygon_is_the_four_corners_in_chained_order(envelope):
    assert len(envelope.outline_nm) == 4
    assert set(envelope.outline_nm) == {
        (_nm(OUTLINE_X0), _nm(OUTLINE_Y0)),
        (_nm(OUTLINE_X1), _nm(OUTLINE_Y0)),
        (_nm(OUTLINE_X1), _nm(OUTLINE_Y1)),
        (_nm(OUTLINE_X0), _nm(OUTLINE_Y1)),
    }
    # Chained: every consecutive pair shares an axis (it is a rectangle).
    pts = list(envelope.outline_nm)
    for a, b in zip(pts, pts[1:] + pts[:1], strict=True):
        assert a[0] == b[0] or a[1] == b[1], "outline points are not chained"


def test_thickness_comes_from_the_board_file(envelope):
    # The fixture says (general (thickness 1.6)).
    assert envelope.thickness_nm == _nm(1.6)
    assert mm(1.6) == DEFAULT_BOARD_THICKNESS_NM


# ------------------------------------------------------- footprint extents


def test_c2_extent_is_anchor_plus_courtyard(envelope):
    # Raw fixture literals: C2 is (at 2.35 6.8), courtyard fp_lines spanning
    # x in [-1.7, 1.7], y in [-0.98, 0.98] -- no rotation.
    part = find_part(envelope, "C2")
    assert part is not None
    assert part.x_min_nm == _nm(2.35) + _nm(-1.7)
    assert part.x_max_nm == _nm(2.35) + _nm(1.7)
    assert part.y_min_nm == _nm(6.8) + _nm(-0.98)
    assert part.y_max_nm == _nm(6.8) + _nm(0.98)


def test_rotated_180_extent_c3(envelope):
    # C3 is (at 0.95 4.4 180). Its courtyard is symmetric, so a correct
    # extractor gives the same box as unrotated -- and an extractor that
    # dropped the anchor or double-flipped would not (the issue-9 bug class).
    part = find_part(envelope, "C3")
    assert part is not None
    assert part.x_min_nm == _nm(0.95) + _nm(-1.7)
    assert part.x_max_nm == _nm(0.95) + _nm(1.7)
    assert part.y_min_nm == _nm(4.4) + _nm(-0.98)
    assert part.y_max_nm == _nm(4.4) + _nm(0.98)


def test_rotated_90_extent_is_computed_in_the_board_frame(tmp_path):
    # Synthetic footprint at (10, 20) rotated 90, courtyard corners
    # (-1,-0.5)..(3,0.5). KiCad's CCW-on-screen rotation maps a local (x, y)
    # to (y, -x), so the rotated corners are (-0.5,1),(-0.5,-3),(0.5,-3),
    # (0.5,1): local bbox x[-0.5,0.5], y[-3,1].
    envelope = board_envelope(_synthetic_board(tmp_path, angle=90.0))
    (part,) = envelope.parts
    assert part.ref == "X1"
    assert part.x_min_nm == _nm(10) + _nm(-0.5)
    assert part.x_max_nm == _nm(10) + _nm(0.5)
    assert part.y_min_nm == _nm(20) + _nm(-3.0)
    assert part.y_max_nm == _nm(20) + _nm(1.0)


def test_every_fixture_part_is_present_inside_the_outline(envelope):
    assert len(envelope.parts) == 11
    for part in envelope.parts:
        assert part.x_min_nm < part.x_max_nm
        assert part.y_min_nm < part.y_max_nm
        assert part.x_min_nm >= envelope.x_min_nm
        assert part.y_max_nm <= envelope.y_max_nm


# ----------------------------------------------------------------- heights


def test_known_classes_do_not_default():
    for name, low, high in [
        ("Package_QFP:LQFP-48_7x7mm_P0.5mm", 1.0, 2.0),
        ("Package_TO_SOT_SMD:SOT-223-3_TabPin2", 1.5, 2.5),
        ("Capacitor_SMD:C_0805_2012Metric", 1.0, 2.0),
        ("Package_SON:WSON-8-1EP_2x2mm_P0.5mm_EP0.9x1.6mm", 0.5, 1.5),
    ]:
        height, was_default = height_for(name)
        assert not was_default, name
        assert _nm(low) <= height <= _nm(high), name


def test_unknown_class_defaults_and_says_so():
    height, was_default = height_for("Nonsense:Totally_Unknown_Part")
    assert height == DEFAULT_HEIGHT_NM
    assert was_default


# Holes, fiducials and test points are the only classes with nothing above
# the board; every real package must be strictly positive.
_FLAT_CLASSES = {"MountingHole", "Fiducial", "TestPoint"}


def test_table_is_all_positive_integer_nm():
    for key, value in HEIGHTS_NM.items():
        assert isinstance(value, int), key
        if key in _FLAT_CLASSES:
            assert value == 0, key
        else:
            assert value > 0, key
    assert isinstance(DEFAULT_HEIGHT_NM, int) and DEFAULT_HEIGHT_NM > 0


def test_no_fixture_part_needs_the_default(envelope):
    defaulted = [p.ref for p in envelope.parts if p.height_default]
    assert defaulted == []
    assert envelope.max_height_nm == max(p.height_nm for p in envelope.parts)


def test_unknown_footprint_class_surfaces_the_default_flag(tmp_path):
    envelope = board_envelope(_synthetic_board(tmp_path))
    (part,) = envelope.parts
    assert part.height_default
    assert part.height_nm == DEFAULT_HEIGHT_NM
    assert envelope.max_height_nm == DEFAULT_HEIGHT_NM


def test_bottom_side_part_is_carried_and_split_out(tmp_path):
    # A B.Cu footprint hangs *below* the board: its height must land in
    # max_height_bottom_nm and must not leak into the top-side budget.
    envelope = board_envelope(_synthetic_board(tmp_path, layer="B.Cu"))
    (part,) = envelope.parts
    assert part.side is Layer.BOTTOM
    assert envelope.max_height_nm == 0
    assert envelope.max_height_bottom_nm == DEFAULT_HEIGHT_NM


def test_top_side_is_the_default_and_fills_max_height(tmp_path):
    envelope = board_envelope(_synthetic_board(tmp_path, layer="F.Cu"))
    (part,) = envelope.parts
    assert part.side is Layer.TOP
    assert envelope.max_height_nm == DEFAULT_HEIGHT_NM
    assert envelope.max_height_bottom_nm == 0


def test_fixture_parts_are_all_top_side(envelope):
    # The STM32 fixture is a single-sided board; the split must say so.
    assert all(p.side is Layer.TOP for p in envelope.parts)
    assert envelope.max_height_bottom_nm == 0
    assert envelope.max_height_nm == max(p.height_nm for p in envelope.parts)


def test_ref_keyed_height_override_wins(tmp_path):
    envelope = board_envelope(
        _outlined_fixture(tmp_path), heights={"C2": _nm(7.0)}
    )
    part = find_part(envelope, "C2")
    assert part is not None
    assert part.height_nm == _nm(7.0)
    assert not part.height_default


def test_class_keyed_height_override_extends_the_table(tmp_path):
    envelope = board_envelope(
        _synthetic_board(tmp_path), heights={"Bar_Widget": _nm(4.5)}
    )
    (part,) = envelope.parts
    assert part.height_nm == _nm(4.5)
    assert not part.height_default


def test_find_part_returns_none_for_absent_ref(envelope):
    assert find_part(envelope, "J99") is None

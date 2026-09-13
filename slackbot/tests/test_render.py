"""The board preview.

The assertions that matter are about coordinates. A preview that silently
mirrors the board looks perfectly plausible in a channel and disagrees with the
``.kicad_pcb`` posted beside it, which is exactly the failure this repository
treats as a bug class rather than a cosmetic defect.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import pytest

from slackbot.render import MARGIN_MM, render_board, render_png, render_svg
from slackbot.tests.fakes import fake_board


def _rects(svg: str) -> list[dict[str, str]]:
    root = ET.fromstring(svg)
    return [el.attrib for el in root.iter("{http://www.w3.org/2000/svg}rect")]


def test_svg_is_well_formed_and_sized_for_the_board():
    svg = render_svg(fake_board())
    root = ET.fromstring(svg)
    # 20 x 12 mm board plus a margin on each side, at some positive scale.
    width = float(root.attrib["width"])
    height = float(root.attrib["height"])
    assert width > height > 0
    assert abs(width / height - (20 + 2 * MARGIN_MM) / (12 + 2 * MARGIN_MM)) < 0.01


def test_every_part_and_pad_is_drawn():
    svg = render_svg(fake_board())
    rects = _rects(svg)
    # background + board + 2 courtyards + 4 pads
    assert len(rects) == 8
    assert svg.count("<text") == 3  # two refs and the caption


def test_reference_designators_appear():
    svg = render_svg(fake_board())
    assert ">R1<" in svg and ">C1<" in svg


def test_exactly_one_y_flip():
    """The negative scale must appear once, in the single group transform.

    A second flip anywhere below it would cancel this one on some elements and
    not others, which is how a board picture ends up mirrored in part.
    """
    assert len(re.findall(r"scale\([^)]*-", render_svg(fake_board()))) == 1


def test_a_rotated_part_has_swapped_courtyard_extents():
    """C1 is rotated; its 2.0 x 1.2 mm courtyard must be drawn 1.2 x 2.0."""
    svg = render_svg(fake_board())
    boxes = {
        (round(float(r["width"]), 2), round(float(r["height"]), 2))
        for r in _rects(svg)
        if r.get("stroke-width") == "0.12"
    }
    assert (2.0, 1.2) in boxes  # R1, unrotated
    assert (1.2, 2.0) in boxes  # C1, rotated


def test_labels_are_not_inside_the_flipped_group():
    """Text drawn inside the flip renders upside down."""
    svg = render_svg(fake_board())
    body = svg.split("</g>")[0]
    assert "<text" not in body


def test_a_ref_with_markup_characters_is_escaped():
    board = fake_board()
    board.parts[0].ref = "R<1>&"
    svg = render_svg(board)
    assert "R&lt;1&gt;&amp;" in svg
    ET.fromstring(svg)  # still parses


def test_render_board_prefers_png_when_pillow_is_present():
    image = render_board(fake_board(), stem="run-abc")
    if render_png(fake_board()) is None:
        assert image.filename == "run-abc.svg"
        assert image.mimetype == "image/svg+xml"
    else:
        assert image.filename == "run-abc.png"
        assert image.is_png
        assert image.content.startswith(b"\x89PNG")


def test_png_render_is_skipped_cleanly_without_pillow(monkeypatch):
    """The optional dependency is genuinely optional."""
    import builtins

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.startswith("PIL"):
            raise ImportError("no PIL")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    assert render_png(fake_board()) is None
    assert render_board(fake_board()).mimetype == "image/svg+xml"


@pytest.mark.parametrize("size", [(1.0, 1.0), (400.0, 300.0)])
def test_extreme_board_sizes_stay_renderable(size):
    from silkscreen.units import mm

    board = fake_board()
    board.width_nm, board.height_nm = mm(size[0]), mm(size[1])
    root = ET.fromstring(render_svg(board))
    assert 0 < float(root.attrib["width"]) < 12_000

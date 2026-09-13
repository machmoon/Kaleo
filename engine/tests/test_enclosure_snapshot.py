"""Tests for :mod:`silkscreen.enclosure.snapshot`, the software renderer.

Gated on the ``cad`` extra (``needs_build123d``); the models come from the
hand-built helpers in ``test_enclosure_kernel.py`` so the renderer is
exercised on geometry this suite states, whatever ``cad.build_enclosure``
does. The assertions are deliberately about the *images* -- size, not blank,
distinct per view, byte-stable -- not about pixels at coordinates, which
would pin the camera maths rather than the contract.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from silkscreen.enclosure import snapshot
from silkscreen.enclosure.cad import kernel_available
from silkscreen.enclosure.snapshot import VIEWS, render_grid, render_packet
from test_enclosure_kernel import OH, hand_model

needs_build123d = pytest.mark.skipif(
    not kernel_available(), reason="build123d not installed (pip install -e '.[cad]')"
)


def _variance(png: bytes) -> float:
    import numpy as np
    from PIL import Image

    return float(np.asarray(Image.open(io.BytesIO(png)).convert("L")).var())


def test_views_are_frozen_and_module_imports_without_the_kernel():
    assert VIEWS == ("iso", "iso_opposite", "top", "front", "section")
    assert snapshot.MAX_TRIANGLES > 0


@needs_build123d
def test_packet_writes_five_distinct_non_blank_pngs_at_the_requested_size(
    tmp_path: Path,
):
    from PIL import Image

    size = (400, 300)
    paths = render_packet(hand_model(), tmp_path / "packet", size=size)
    assert tuple(p.name for p in paths) == tuple(f"{v}.png" for v in VIEWS)
    blobs = []
    for path in paths:
        assert path.exists()
        with Image.open(path) as image:
            assert image.format == "PNG"
            assert image.size == size
        blob = path.read_bytes()
        assert _variance(blob) > 0, f"{path.name} is blank"
        blobs.append(blob)
    assert len(set(blobs)) == len(blobs), "two views rendered identically"


@needs_build123d
def test_packet_is_byte_deterministic(tmp_path: Path):
    first = [p.read_bytes() for p in render_packet(hand_model(), tmp_path / "a")]
    second = [p.read_bytes() for p in render_packet(hand_model(), tmp_path / "b")]
    assert first == second


@needs_build123d
def test_grid_is_png_bytes_of_the_requested_size_and_deterministic():
    from PIL import Image

    size = (600, 450)
    png = render_grid(hand_model(), size=size)
    assert isinstance(png, bytes) and png.startswith(b"\x89PNG\r\n\x1a\n")
    with Image.open(io.BytesIO(png)) as image:
        assert image.size == size
    assert _variance(png) > 0
    assert render_grid(hand_model(), size=size) == png


@needs_build123d
def test_lid_none_renders_and_differs_from_the_lidded_case(tmp_path: Path):
    lidless = hand_model(no_lid=True, outer_z=OH)
    paths = render_packet(lidless, tmp_path / "nolid", size=(320, 240))
    assert len(paths) == len(VIEWS)
    lidded = render_grid(hand_model(), size=(320, 240))
    assert render_grid(lidless, size=(320, 240)) != lidded
    for path in paths:
        assert _variance(path.read_bytes()) > 0


@needs_build123d
def test_section_view_differs_from_front_view(tmp_path: Path):
    """The section cuts the base open through the tallest part; the front view
    shows the closed wall. If the split were a no-op the two would match."""
    packet = render_packet(hand_model(), tmp_path / "s", size=(320, 240))
    paths = dict(zip(VIEWS, packet, strict=True))
    assert paths["front"].read_bytes() != paths["section"].read_bytes()

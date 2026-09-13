"""Draw a placed board as an image Slack can show inline.

Geometry is not recomputed here. ``service.app._placements_dict`` already
resolves every rectangle -- courtyards and pads, absolute, with rotation
applied, in the solver's Y-up frame -- and this module reuses it rather than
repeating the pad-offset and rotation maths. Duplicating that transform is the
repository's named board-destroying bug class: a second copy that disagrees
with the first draws a picture the board file does not match, and nothing
raises. (That the shared function is private is a wart on the service's
surface, not a reason to write it twice; see the PR description.)

The single Y flip into image space happens once, in the SVG group transform --
the same rule ``frontend/src/lib/board.js`` follows on the client side.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from service.app import _placements_dict  # noqa: E402

__all__ = ["BoardImage", "render_svg", "render_png", "render_board"]

#: Deliberately dark in every context, matching the web UI's ``--board-*``
#: tokens: a board is a board, and a light-mode preview of one reads as wrong.
BG = "#101613"
SOLDERMASK = "#0f3d2e"
EDGE = "#7fd6b0"
COURTYARD = "#2f6f57"
COURTYARD_FILL = "#17342a"
PAD = "#d9a441"
SILK = "#e8efe9"
LABEL = "#9fb3a8"

#: Millimetres of dark board left around the outline, so parts on the edge are
#: not clipped by the image border.
MARGIN_MM = 3.0
#: Pixels per millimetre, clamped so a tiny board is not a stamp and a large
#: one does not become a megabyte of PNG.
MIN_SCALE, MAX_SCALE, TARGET_PX = 4.0, 24.0, 900.0


@dataclass(frozen=True)
class BoardImage:
    """A rendered preview, plus the filename it should carry into Slack."""

    filename: str
    content: bytes
    mimetype: str

    @property
    def is_png(self) -> bool:
        return self.mimetype == "image/png"


def _scale_for(width_mm: float, height_mm: float) -> float:
    span = max(width_mm, height_mm, 1.0) + 2 * MARGIN_MM
    return max(MIN_SCALE, min(MAX_SCALE, TARGET_PX / span))


def render_svg(board, *, title: str = "") -> str:
    """The placed board as SVG. Pure text, no dependencies, always available."""
    data = _placements_dict(board)
    board_w, board_h = data["board_mm"]
    scale = _scale_for(board_w, board_h)
    width_px = round((board_w + 2 * MARGIN_MM) * scale)
    height_px = round((board_h + 2 * MARGIN_MM) * scale)

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width_px}" '
        f'height="{height_px}" viewBox="0 0 {width_px} {height_px}">',
        f'<rect width="{width_px}" height="{height_px}" fill="{BG}"/>',
    ]
    # The only frame change in this module. Everything below is written in the
    # solver's millimetres, Y up; this transform is what puts it on screen.
    out.append(
        f'<g transform="translate({MARGIN_MM * scale:.2f},'
        f'{(board_h + MARGIN_MM) * scale:.2f}) scale({scale:.4f},{-scale:.4f})">'
    )
    out.append(
        f'<rect x="0" y="0" width="{board_w:.3f}" height="{board_h:.3f}" '
        f'fill="{SOLDERMASK}" stroke="{EDGE}" stroke-width="0.2"/>'
    )

    labels: list[tuple[float, float, str]] = []
    for part in data["parts"]:
        x, y, w, h = part["courtyard_mm"]
        out.append(
            f'<rect x="{x:.3f}" y="{y:.3f}" width="{w:.3f}" height="{h:.3f}" '
            f'fill="{COURTYARD_FILL}" stroke="{COURTYARD}" stroke-width="0.12"/>'
        )
        for pad in part["pads"]:
            px, py, pw, ph = pad["rect_mm"]
            out.append(
                f'<rect x="{px:.3f}" y="{py:.3f}" width="{pw:.3f}" '
                f'height="{ph:.3f}" fill="{PAD}"/>'
            )
        labels.append((x + w / 2, y + h / 2, str(part["ref"])))
    out.append("</g>")

    # Text is placed in image space rather than inside the flipped group, so
    # reference designators are not rendered upside down.
    for mm_x, mm_y, ref in labels:
        px = (mm_x + MARGIN_MM) * scale
        py = (board_h + MARGIN_MM - mm_y) * scale
        size = max(7.0, min(13.0, scale * 0.85))
        out.append(
            f'<text x="{px:.2f}" y="{py:.2f}" fill="{SILK}" font-size="{size:.1f}" '
            'font-family="monospace" text-anchor="middle" '
            f'dominant-baseline="central">{escape(ref)}</text>'
        )

    caption = title or f"{board_w:.2f} x {board_h:.2f} mm"
    out.append(
        f'<text x="{MARGIN_MM * scale:.2f}" y="{height_px - 4}" fill="{LABEL}" '
        f'font-size="11" font-family="monospace">{escape(caption)}</text>'
    )
    out.append("</svg>")
    return "\n".join(out)


def render_png(board, *, title: str = "") -> bytes | None:
    """The same board as PNG, or None when Pillow is not installed.

    Slack previews a PNG inline and offers an SVG as a download, so the PNG is
    worth an optional dependency -- but only an optional one, since the board
    file, not the picture, is the deliverable.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    import io

    data = _placements_dict(board)
    board_w, board_h = data["board_mm"]
    scale = _scale_for(board_w, board_h)
    width_px = max(1, round((board_w + 2 * MARGIN_MM) * scale))
    height_px = max(1, round((board_h + 2 * MARGIN_MM) * scale))

    image = Image.new("RGB", (width_px, height_px), BG)
    draw = ImageDraw.Draw(image)

    def box(
        x: float, y: float, w: float, h: float
    ) -> tuple[float, float, float, float]:
        """Millimetres, Y up -> pixels, Y down. The one flip, again once."""
        left = (x + MARGIN_MM) * scale
        top = (board_h + MARGIN_MM - (y + h)) * scale
        return (left, top, left + w * scale, top + h * scale)

    draw.rectangle(box(0, 0, board_w, board_h), fill=SOLDERMASK, outline=EDGE)
    for part in data["parts"]:
        x, y, w, h = part["courtyard_mm"]
        draw.rectangle(box(x, y, w, h), fill=COURTYARD_FILL, outline=COURTYARD)
        for pad in part["pads"]:
            draw.rectangle(box(*pad["rect_mm"]), fill=PAD)
    for part in data["parts"]:
        x, y, w, h = part["courtyard_mm"]
        left, top, right, bottom = box(x, y, w, h)
        draw.text(
            ((left + right) / 2, (top + bottom) / 2),
            str(part["ref"]),
            fill=SILK,
            anchor="mm",
        )

    caption = title or f"{board_w:.2f} x {board_h:.2f} mm"
    draw.text((4, height_px - 14), caption, fill=LABEL)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def render_board(board, *, stem: str = "board", title: str = "") -> BoardImage:
    """Render the best preview available: PNG if Pillow is here, else SVG."""
    png = render_png(board, title=title)
    if png is not None:
        return BoardImage(f"{stem}.png", png, "image/png")
    svg = render_svg(board, title=title)
    return BoardImage(f"{stem}.svg", svg.encode("utf-8"), "image/svg+xml")

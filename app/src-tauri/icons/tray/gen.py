# Menu-bar (tray) glyphs for Kaleo/Ada, drawn with Pillow (system python3;
# `python3 gen.py` in this directory regenerates all six files).
#
# Two states: an outline ring (Ada muted / idle) and a filled orb (the
# microphone is open) -- the overlay's "orb", not the app logo, because a
# 22 px K on a green square reads as nothing at 18 pt in a menu bar.
#
# The `*Template*.png` files are macOS template images: black on
# transparent, alpha only, so the system tints them for a light or dark
# menu bar. The `*-colour.png` files are the copper variants for Windows and
# Linux trays, which have no template concept. The shape is rendered as an
# 8x supersampled mask and *then* given one flat colour, so anti-aliasing
# lives entirely in the alpha channel and no edge pixel is a different hue.
from PIL import Image, ImageDraw

SS = 8  # supersample factor for antialiasing


def mask(size: int, filled: bool) -> Image.Image:
    big = size * SS
    im = Image.new("L", (big, big), 0)
    d = ImageDraw.Draw(im)
    c = big / 2
    r = big * 0.40  # outer radius: ~80% of the box
    stroke = big * 0.11  # ring stroke (~2.4 px at 22)
    box = [c - r, c - r, c + r, c + r]
    if filled:
        d.ellipse(box, fill=255)
    else:
        d.ellipse(box, outline=255, width=int(round(stroke)))
    return im.resize((size, size), Image.LANCZOS)


def glyph(size: int, filled: bool, colour: tuple[int, int, int]) -> Image.Image:
    im = Image.new("RGBA", (size, size), colour + (255,))
    im.putalpha(mask(size, filled))
    return im


BLACK = (0, 0, 0)
COPPER = (0xB8, 0x6D, 0x38)  # the trace colour in ../../../images/kaleo-logo.svg
for name, filled in (("idle", False), ("live", True)):
    glyph(22, filled, BLACK).save(f"{name}Template.png")
    glyph(44, filled, BLACK).save(f"{name}Template@2x.png")
    glyph(32, filled, COPPER).save(f"{name}-colour.png")

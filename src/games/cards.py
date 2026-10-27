from __future__ import annotations

import functools
import io

import discord
from PIL import Image

from src.cards import BG, GOLD, draw_text, icon

ICON_SPOTS = (
    ("joker", 60, 70, 150),
    ("slot_machine", 250, 40, 120),
    ("game_die", 850, 40, 120),
    ("rocket", 1010, 90, 140),
)


@functools.cache
def banner_png() -> bytes:
    img = Image.new("RGBA", (1200, 300), (*BG, 255))
    for name, x, y, size in ICON_SPOTS:
        img.alpha_composite(icon(name, size), (x, y))
    draw_text(img, (600, 150), "遊戲大廳", 110, GOLD, bold=True, anchor="mm")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def banner_file() -> discord.File:
    return discord.File(io.BytesIO(banner_png()), "hub.png")

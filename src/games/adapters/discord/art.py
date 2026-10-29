from __future__ import annotations

import asyncio
import functools
import io
import math
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import discord
from PIL import Image, ImageDraw, ImageFont

from src.games.domain.catalog import GAMES

ASSETS = Path(__file__).parents[3] / "assets"
TC_FONT = ASSETS / "fonts" / "NotoSansTC-VF.ttf"
BG, GOLD = (31, 25, 51), (232, 196, 92)

_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cards")


@functools.cache
def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    face = ImageFont.truetype(str(TC_FONT), size)
    face.set_variation_by_name("Bold" if bold else "Regular")
    return face


def draw_text(
    img: Image.Image, xy: tuple[float, float], text: str, size: int, fill, *, bold: bool = False, anchor: str = "la"
) -> float:
    face = font(size, bold)
    width = face.getlength(text)
    ascent, descent = face.getmetrics()
    x = xy[0] - width * {"l": 0, "m": 0.5, "r": 1}[anchor[0]]
    baseline = xy[1] + {"a": ascent, "t": ascent, "m": (ascent - descent) / 2, "s": 0, "d": -descent}[anchor[1]]
    ImageDraw.Draw(img).text((x, baseline), text, font=face, fill=fill, anchor="ls")
    return width


@functools.cache
def _icon(name: str, size: int) -> Image.Image:
    return Image.open(ASSETS / "fluent" / f"{name}.png").convert("RGBA").resize((size, size), Image.LANCZOS)


def icon(name: str, size: int) -> Image.Image:
    return _icon(name, size).copy()


async def render(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(_worker, fn, *args)


BANNER_SIZE = (1200, 300)
BANNER_ICONS = (("joker", "slot_machine"), ("game_die", "rocket"))
BANNER_ICON = 120
CAPTION = (196, 186, 222)


def _label(text: str, size: int, fill, *, bold: bool = False) -> Image.Image:
    width = font(size, bold).getlength(text)
    layer = Image.new("RGBA", (math.ceil(width) + size, size * 2))
    draw_text(layer, (size / 2, size / 2), text, size, fill, bold=bold)
    return layer.crop(layer.getbbox())


def banner_png() -> bytes:
    return _banner("・".join(game.title for game in GAMES.values()))


@functools.cache
def _banner(caption: str) -> bytes:
    width, height = BANNER_SIZE
    img = Image.new("RGBA", BANNER_SIZE, (*BG, 255))
    title, subtitle = _label("遊戲大廳", 104, GOLD, bold=True), _label(caption, 36, CAPTION)
    gap = 26
    top = (height - title.height - gap - subtitle.height) // 2
    img.alpha_composite(title, ((width - title.width) // 2, top))
    img.alpha_composite(subtitle, ((width - subtitle.width) // 2, top + title.height + gap))
    middle = top + title.height / 2
    side = (width - title.width) / 2
    space = (side - BANNER_ICON * len(BANNER_ICONS[0])) / (len(BANNER_ICONS[0]) + 1)
    for start, names in zip((0, width - side), BANNER_ICONS):
        for slot, name in enumerate(names):
            image = icon(name, BANNER_ICON)
            left, upper, right, lower = image.getbbox()
            centre = start + space + slot * (BANNER_ICON + space) + BANNER_ICON / 2
            img.alpha_composite(image, (round(centre - (left + right) / 2), round(middle - (upper + lower) / 2)))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def banner_file() -> discord.File:
    return discord.File(io.BytesIO(banner_png()), "hub.png")

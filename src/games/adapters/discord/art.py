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
DEJAVU = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
DEJAVU_BOLD = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
EMOJI_FONT = Path("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf")
CHAIN = (TC_FONT, EMOJI_FONT, DEJAVU)
NOTDEF = "\U0010fffd"
BG, GOLD = (31, 25, 51), (232, 196, 92)

_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cards")


@functools.cache
def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    face = ImageFont.truetype(str(TC_FONT), size)
    face.set_variation_by_name("Bold" if bold else "Regular")
    return face


@functools.cache
def _face(path: Path, size: int) -> ImageFont.FreeTypeFont | None:
    try:
        return ImageFont.truetype(str(path), size)
    except OSError:
        return None


def _ink(face: ImageFont.FreeTypeFont, ch: str) -> bytes:
    im = Image.new("RGBA", (160, 160))
    ImageDraw.Draw(im).text((0, 0), ch, font=face, fill="white", embedded_color=True)
    return im.tobytes()


@functools.cache
def _notdef(path: Path) -> bytes:
    return _ink(_face(path, 109 if path == EMOJI_FONT else 48), NOTDEF)


@functools.cache
def _covers(path: Path, ch: str) -> bool:
    face = _face(path, 109 if path == EMOJI_FONT else 48)
    return face is not None and _ink(face, ch) != _notdef(path)


@functools.lru_cache(maxsize=256)
def _emoji(ch: str, size: int) -> Image.Image | None:
    im = Image.new("RGBA", (160, 160))
    ImageDraw.Draw(im).text((0, 0), ch, font=_face(EMOJI_FONT, 109), embedded_color=True)
    box = im.getbbox()
    if box is None:
        return None
    im = im.crop(box)
    scale = size / 128
    return im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.LANCZOS)


def _runs(text: str, size: int, bold: bool) -> list[tuple[ImageFont.FreeTypeFont | Image.Image, str, float]]:
    groups: list[list] = []
    for ch in text:
        src = next((path for path in CHAIN if _covers(path, ch)), None)
        if src is None:
            continue
        if src != EMOJI_FONT and groups and groups[-1][0] == src:
            groups[-1][1] += ch
        else:
            groups.append([src, ch])
    runs = []
    for src, chunk in groups:
        if src == EMOJI_FONT:
            if (im := _emoji(chunk, size)) is not None:
                runs.append((im, chunk, im.width + size * 0.08))
            continue
        face = font(size, bold) if src == TC_FONT else (bold and _face(DEJAVU_BOLD, size)) or _face(DEJAVU, size)
        runs.append((face, chunk, face.getlength(chunk)))
    return runs


def draw_text(
    img: Image.Image, xy: tuple[float, float], text: str, size: int, fill, *, bold: bool = False, anchor: str = "la"
) -> float:
    runs = _runs(text, size, bold)
    width = sum(w for *_, w in runs)
    ascent, descent = font(size, bold).getmetrics()
    x = xy[0] - width * {"l": 0, "m": 0.5, "r": 1}[anchor[0]]
    baseline = xy[1] + {"a": ascent, "t": ascent, "m": (ascent - descent) / 2, "s": 0, "d": -descent}[anchor[1]]
    draw = ImageDraw.Draw(img)
    for src, chunk, w in runs:
        if isinstance(src, Image.Image):
            top = baseline - size * 0.38 - src.height / 2
            img.alpha_composite(src, (max(0, round(x)), max(0, round(top))))
        else:
            draw.text((x, baseline), chunk, font=src, fill=fill, anchor="ls")
        x += w
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
    width = sum(w for *_, w in _runs(text, size, bold))
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

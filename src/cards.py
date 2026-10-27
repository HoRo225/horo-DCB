from __future__ import annotations

import asyncio
import functools
import io
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import aiohttp
import discord
from PIL import Image, ImageDraw, ImageFont

ASSETS = Path(__file__).parent / "assets"
TC_FONT = ASSETS / "fonts" / "NotoSansTC-VF.ttf"
DEJAVU = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
DEJAVU_BOLD = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
EMOJI_FONT = Path("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf")
CHAIN = (TC_FONT, EMOJI_FONT, DEJAVU)
NOTDEF = "\U0010fffd"
BG, GOLD, WHITE, MUTED, TRACK = (31, 25, 51), (232, 196, 92), (255, 255, 255), (201, 195, 230), (58, 53, 80)
AA = 4
AVATAR_CACHE = 64

_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cards")
_avatars: dict[str, bytes] = {}


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


def text_width(text: str, size: int, bold: bool = False) -> float:
    return sum(w for *_, w in _runs(text, size, bold))


def clip(text: str, size: int, width: float, bold: bool = False) -> str:
    if text_width(text, size, bold) <= width:
        return text
    cut = text
    while cut and text_width(cut + "…", size, bold) > width:
        cut = cut[:-1]
    return cut + "…"


def fit(text: str, size: int, width: float, bold: bool = False, min_size: int = 40) -> tuple[str, int]:
    while size > min_size and text_width(text, size, bold) > width:
        size = max(min_size, size - 2)
    return clip(text, size, width, bold), size


def aa(size: tuple[int, int], paint) -> Image.Image:
    big = Image.new("RGBA", (size[0] * AA, size[1] * AA))
    paint(ImageDraw.Draw(big), AA)
    return big.resize(size, Image.LANCZOS)


@functools.cache
def _icon(name: str, size: int) -> Image.Image:
    return Image.open(ASSETS / "fluent" / f"{name}.png").convert("RGBA").resize((size, size), Image.LANCZOS)


def icon(name: str, size: int) -> Image.Image:
    return _icon(name, size).copy()


def avatar_circle(data: bytes | Image.Image | None, size: int) -> Image.Image:
    ring = max(2, round(size / 20))
    inner = size - 2 * ring
    disc = Image.new("RGBA", (inner, inner), (*TRACK, 255))
    try:
        if data is not None:
            face = data if isinstance(data, Image.Image) else Image.open(io.BytesIO(data))
            disc.alpha_composite(face.convert("RGBA").resize((inner, inner), Image.LANCZOS))
    except OSError:
        pass
    out = aa((size, size), lambda d, s: d.ellipse([0, 0, size * s - 1, size * s - 1], fill=GOLD))
    mask = aa((inner, inner), lambda d, s: d.ellipse([0, 0, inner * s - 1, inner * s - 1], fill=WHITE)).getchannel("A")
    out.paste(disc, (ring, ring), mask)
    return out


def _canvas(width: int, height: int) -> Image.Image:
    return Image.new("RGBA", (width, height), (*BG, 255))


T3_FACES = 5


def t3(title: str, sub: str, avatars: list, art: str, filename: str) -> discord.File:
    img = _canvas(960, 240)
    img.alpha_composite(icon(art, 150), (36, 45))
    faces = [avatar_circle(a, 72) for a in avatars[:T3_FACES]]
    if len(avatars) > T3_FACES:
        chip = avatar_circle(None, 72)
        draw_text(chip, (36, 36), clip(f"+{len(avatars) - T3_FACES}", 30, 56, True), 30, WHITE, bold=True, anchor="mm")
        faces.append(chip)
    left = 894 - 42 * len(faces)
    for i, face in enumerate(faces):
        img.alpha_composite(face, (left + 42 * i, 84))
    x, w = 210, left - 24 - 210
    text, size = fit(title, 56, w, True)
    draw_text(img, (x, 96), text, size, GOLD, bold=True, anchor="lm")
    draw_text(img, (x, 160), clip(sub, 30, w), 30, MUTED, anchor="lm")
    return to_file(img, filename)


async def fetch_avatar(asset: discord.Asset) -> bytes | None:
    asset = asset.replace(size=256, format="png")
    if (data := _avatars.get(asset.url)) is not None:
        return data
    try:
        async with asyncio.timeout(1.5):
            data = await asset.read()
    except (discord.HTTPException, aiohttp.ClientError, TimeoutError):
        return None
    if len(_avatars) >= AVATAR_CACHE:
        _avatars.pop(next(iter(_avatars)))
    _avatars[asset.url] = data
    return data


async def render(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(_worker, fn, *args)


def to_file(img: Image.Image, filename: str) -> discord.File:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return discord.File(buf, filename=filename)

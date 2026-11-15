import io
import re
import sys
import zlib
from pathlib import Path

import resvg_py
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from horo_dcb.blackjack import RANKS, SUITS  # noqa: E402
from horo_dcb.picture import ASSETS, CARD_H, CARD_W, CELL, COLOURS, GREEN, PANEL, WELL  # noqa: E402
from horo_dcb.slots import STRIP, SYMBOLS  # noqa: E402

ART = ROOT / "art" / "games"
RGB = {PANEL: (30, 31, 34), WELL: (49, 51, 56), GREEN: (35, 165, 90)}
SYMBOL_SIZE, SEVEN_SIZE, GLYPH = 72, 76, 288
BLUR_SQUASH = 6


def svg(path: Path, width: int) -> Image.Image:
    text = path.read_text(encoding="utf-8")
    end = text.index(">", text.index("<svg"))
    head = re.sub(r'((?:width|height)="[\d.]+)(?:in|mm|pt)"', r'\1"', text[:end])
    return Image.open(io.BytesIO(bytes(resvg_py.svg_to_bytes(svg_string=head + text[end:], width=width)))).convert("RGBA")


def fit(image: Image.Image, width: int, height: int) -> Image.Image:
    scale = min(width / image.width, height / image.height)
    size = (round(image.width * scale), round(image.height * scale))
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    canvas.alpha_composite(image.resize(size, Image.Resampling.LANCZOS), ((width - size[0]) // 2, (height - size[1]) // 2))
    return canvas


def cards() -> bytes:
    names = [f"{'T' if rank == '10' else rank}{suit}" for suit in SUITS for rank in RANKS] + ["back"]
    return b"".join(fit(svg(ART / "cards" / f"{name}.svg", CARD_W * 6), CARD_W, CARD_H).tobytes() for name in names)


def gradient(top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    image = Image.new("RGBA", (GLYPH, GLYPH))
    draw = ImageDraw.Draw(image)
    for y in range(GLYPH):
        t = y / (GLYPH - 1)
        draw.line(((0, y), (GLYPH, y)), fill=tuple(round(a + (b - a) * t) for a, b in zip(top, bottom, strict=True)) + (255,))
    return image


def glyph(stroke: int) -> Image.Image:
    path = str(ART / "font" / "AlfaSlabOne-Regular.ttf")
    mask = Image.new("L", (GLYPH, GLYPH), 0)
    draw = ImageDraw.Draw(mask)
    face = ImageFont.truetype(path, 230)
    left, top, right, bottom = draw.textbbox((0, 0), "7", font=face, stroke_width=stroke)
    if right - left > 220 or bottom - top > 220:
        face = ImageFont.truetype(path, int(230 * min(220 / (right - left), 220 / (bottom - top))))
        left, top, right, bottom = draw.textbbox((0, 0), "7", font=face, stroke_width=stroke)
    draw.text(((GLYPH - (right - left)) // 2 - left, (GLYPH - (bottom - top)) // 2 - top - 6), "7", font=face, fill=255, stroke_width=stroke, stroke_fill=255)
    return mask


def layer(colour: Image.Image, mask: Image.Image, offset: int = 0) -> Image.Image:
    out = Image.new("RGBA", (GLYPH, GLYPH), (0, 0, 0, 0))
    out.paste(colour, (0, 0), ImageChops.offset(mask, 0, offset))
    return out


def seven() -> Image.Image:
    inner, outer = glyph(0), glyph(11)
    image = Image.new("RGBA", (GLYPH, GLYPH), (0, 0, 0, 0))
    shadow = Image.new("RGBA", (GLYPH, GLYPH), (0, 0, 0, 0))
    shadow.paste((0, 0, 0, 110), (0, 0), ImageChops.offset(outer, 0, 22).filter(ImageFilter.GaussianBlur(10)))
    image.alpha_composite(shadow)
    side = Image.new("RGBA", (GLYPH, GLYPH), (150, 84, 0, 255))
    for step in range(16, 0, -1):
        image.alpha_composite(layer(side, outer, step))
    image.alpha_composite(layer(gradient((255, 233, 138), (232, 150, 24)), outer))
    image.alpha_composite(layer(gradient((255, 112, 96), (214, 24, 44)), inner))
    fade = gradient((255, 255, 255), (0, 0, 0)).convert("L").point(lambda value: max(0, value * 2 - 255))
    shine = Image.new("RGBA", (GLYPH, GLYPH), (255, 255, 255, 0))
    shine.putalpha(ImageChops.multiply(inner.filter(ImageFilter.MinFilter(9)), fade).point(lambda value: value * 120 // 255).filter(ImageFilter.GaussianBlur(3)))
    image.alpha_composite(shine)
    box = image.getbbox()
    return fit(image.crop(box) if box else image, SEVEN_SIZE, SEVEN_SIZE)


def tile(name: str) -> Image.Image:
    symbol = seven() if name == "seven" else fit(Image.open(ART / "symbols" / f"{name}.png").convert("RGBA"), SYMBOL_SIZE, SYMBOL_SIZE)
    cell = Image.new("RGBA", (CELL, CELL), RGB[WELL] + (255,))
    cell.alpha_composite(symbol, ((CELL - symbol.width) // 2, (CELL - symbol.height) // 2))
    return cell


def reel() -> bytes:
    tiles = {name: tile(name) for name in SYMBOLS}
    height = CELL * len(STRIP)
    sharp = Image.new("RGBA", (CELL, height))
    for index, name in enumerate(STRIP):
        sharp.paste(tiles[name], (0, index * CELL))
    wrapped = Image.new("RGBA", (CELL, height + 2 * CELL))
    wrapped.paste(sharp, (0, CELL))
    wrapped.paste(sharp.crop((0, height - CELL, CELL, height)), (0, 0))
    wrapped.paste(sharp.crop((0, 0, CELL, CELL)), (0, height + CELL))
    squashed = wrapped.resize((CELL, (height + 2 * CELL) // BLUR_SQUASH), Image.Resampling.BILINEAR)
    blurred = squashed.resize(wrapped.size, Image.Resampling.BILINEAR).crop((0, CELL, CELL, height + CELL))
    both = Image.new("RGB", (CELL * 2, height))
    both.paste(sharp.convert("RGB"), (0, 0))
    both.paste(blurred.convert("RGB"), (CELL, 0))
    quantized = both.quantize(colors=PANEL, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    palette = bytes((quantized.getpalette() or [])[: PANEL * 3]).ljust(PANEL * 3, b"\x00") + b"".join(bytes(RGB[index]) for index in (PANEL, WELL, GREEN))
    assert len(palette) == COLOURS * 3
    data = quantized.tobytes()
    sharp_rows = b"".join(data[row * CELL * 2 : row * CELL * 2 + CELL] for row in range(height))
    blur_rows = b"".join(data[row * CELL * 2 + CELL : (row + 1) * CELL * 2] for row in range(height))
    return palette + sharp_rows + blur_rows


def main() -> None:
    ASSETS.mkdir(exist_ok=True)
    for name, data in (("cards.bin", cards()), ("reel.bin", reel())):
        (ASSETS / name).write_bytes(zlib.compress(data, 9))
        print(name, (ASSETS / name).stat().st_size)


if __name__ == "__main__":
    main()

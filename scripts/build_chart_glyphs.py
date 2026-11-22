import struct
import sys
import zlib
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

TARGET = Path(__file__).resolve().parent.parent / "src" / "horo_dcb" / "assets" / "chart.bin"
DIGITS = "0123456789.,"
EVERYTHING = DIGITS + "%+-/: ()▲▼K最高低日週月年近個成交量張萬億開收平"
FACES = (
    ("xl", 24, 700, DIGITS + "日週月K"),
    ("m", 15, 700, DIGITS + "%+-() ▲▼"),
    ("s", 12, 400, EVERYTHING),
    ("sb", 12, 700, DIGITS),
)
TOFU = "\U000f0000"


def face(path: Path, size: int, weight: int) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(str(path), size)
    font.set_variation_by_axes([weight])
    return font


def drawn(font: ImageFont.FreeTypeFont, size: int, char: str) -> Image.Image:
    image = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(image).text((size, size * 3), char, 255, font, "ls")
    return image


def build(path: Path) -> bytes:
    out = bytearray(struct.pack("<4sB", b"KGLY", len(FACES)))
    for name, size, weight, chars in FACES:
        font = face(path, size, weight)
        tofu = drawn(font, size, TOFU).tobytes()
        missing = [char for char in chars if char != " " and drawn(font, size, char).tobytes() == tofu]
        if missing:
            sys.exit(f"{path.name} lacks {missing}")
        cap = -round(font.getbbox("0", anchor="ls")[1])
        out += struct.pack("<2sBBH", name.encode().ljust(2), size, cap, len(chars))
        for char in chars:
            image = drawn(font, size, char)
            box = image.getbbox() or (size, size * 3, size, size * 3)
            cut = image.crop(box)
            out += struct.pack("<IHbbBB", ord(char), round(font.getlength(char) * 64), box[0] - size, box[1] - size * 3, cut.width, cut.height)
            out += cut.tobytes()
    return zlib.compress(bytes(out), 9)


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: build_chart_glyphs.py <NotoSansTC-wght.ttf>")
    TARGET.write_bytes(build(Path(sys.argv[1])))
    print(TARGET, TARGET.stat().st_size, "bytes")


if __name__ == "__main__":
    main()

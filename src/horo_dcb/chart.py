import math
import struct
import zlib
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Literal

from .picture import ASSETS, png
from .portfolio import Bar, weekly

type Span = Literal["1m", "3m", "1y"]
type Colour = tuple[int, int, int]
type Glyph = tuple[float, int, int, list[bytes]]

W, H = 800, 480
BG, GRID, AXIS = (0x1E, 0x1F, 0x22), (0x2C, 0x2E, 0x33), (0x3A, 0x3D, 0x44)
TEXT, MUTED, WHITE = (0xDB, 0xDE, 0xE1), (0x94, 0x9B, 0xA4), (0xFF, 0xFF, 0xFF)
UP, DOWN, FLAT = (0xF2, 0x4B, 0x55), (0x2D, 0xBE, 0x7E), (0xB5, 0xBA, 0xC1)
DIM = {UP: (0x8F, 0x32, 0x39), DOWN: (0x26, 0x72, 0x55), FLAT: (0x6A, 0x6E, 0x75)}
LEFT, GUTTER, MAX_BODY = 14, 70, 25
K_TOP, K_BOTTOM, V_TOP, V_BOTTOM = 78, 330, 352, 442
LABEL_ROOM = 24
SPANS: dict[Span, tuple[int, str, str, bool]] = {
    "1m": (1, "日K", "近1個月", False),
    "3m": (3, "日K", "近3個月", False),
    "1y": (12, "週K", "近1年", True),
}


def load_glyphs(path: Path) -> dict[str, tuple[int, dict[str, Glyph]]]:
    blob = zlib.decompress(path.read_bytes())
    magic, faces = struct.unpack_from("<4sB", blob)
    if magic != b"KGLY":
        raise ValueError("chart_glyphs_invalid")
    at, atlas = 5, {}
    for _ in range(faces):
        name, _, cap, count = struct.unpack_from("<2sBBH", blob, at)
        at += 6
        glyphs = {}
        for _ in range(count):
            code, advance, left, top, wide, tall = struct.unpack_from("<IHbbBB", blob, at)
            at += 10
            glyphs[chr(code)] = (advance / 64, left, top, [blob[at + row * wide : at + (row + 1) * wide] for row in range(tall)])
            at += wide * tall
        atlas[name.decode().strip()] = (cap, glyphs)
    return atlas


ATLAS = load_glyphs(ASSETS / "chart.bin")


def width(string: str, face: str = "s") -> float:
    glyphs = ATLAS[face][1]
    return sum(glyphs[char][0] for char in string)


def corner(radius: int) -> list[list[int]]:
    cover = []
    for y in range(radius):
        row = []
        for x in range(radius):
            hits = sum((x + (i + 0.5) / 4 - radius) ** 2 + (y + (j + 0.5) / 4 - radius) ** 2 <= radius * radius for i in range(4) for j in range(4))
            row.append(hits * 255 // 16)
        cover.append(row)
    return cover


class Canvas:
    def __init__(self) -> None:
        self.rows = [bytearray(bytes(BG) * W) for _ in range(H)]

    def rect(self, x0: int, y0: int, x1: int, y1: int, colour: Colour) -> None:
        x0, x1, y0, y1 = max(0, x0), min(W, x1), max(0, y0), min(H, y1)
        if x1 > x0:
            fill = bytes(colour) * (x1 - x0)
            for y in range(y0, y1):
                self.rows[y][x0 * 3 : x1 * 3] = fill

    def blend(self, x: int, y: int, colour: Colour, alpha: int) -> None:
        if 0 <= x < W and 0 <= y < H:
            row, at = self.rows[y], x * 3
            for channel in range(3):
                row[at + channel] = (row[at + channel] * (255 - alpha) + colour[channel] * alpha + 127) // 255

    def rounded(self, x0: int, y0: int, x1: int, y1: int, colour: Colour, radius: int) -> None:
        cover = corner(radius)
        before = {(x, y): bytes(self.rows[y][x * 3 : x * 3 + 3]) for y in (*range(y0, y0 + radius), *range(y1 - radius, y1)) for x in (*range(x0, x0 + radius), *range(x1 - radius, x1))}
        self.rect(x0, y0, x1, y1, colour)
        for (x, y), old in before.items():
            self.rows[y][x * 3 : x * 3 + 3] = old
            self.blend(x, y, colour, cover[min(y - y0, y1 - 1 - y)][min(x - x0, x1 - 1 - x)])

    def dashes(self, x0: int, x1: int, y: int, colour: Colour, on: int = 3, off: int = 4) -> None:
        for x in range(x0, x1, on + off):
            self.rect(x, y, min(x + on, x1), y + 1, colour)

    def vdashes(self, x: int, y0: int, y1: int, colour: Colour, on: int = 3, off: int = 4) -> None:
        for y in range(y0, y1, on + off):
            self.rect(x, y, x + 1, min(y + on, y1), colour)

    def text(self, x: float, y: float, string: str, colour: Colour, face: str = "s", anchor: str = "ls") -> float:
        cap, glyphs = ATLAS[face]
        if anchor[0] != "l":
            x -= width(string, face) / (2 if anchor[0] == "m" else 1)
        baseline = round(y + {"s": 0, "m": cap / 2, "t": cap}[anchor[1]])
        for char in string:
            advance, left, top, bitmap = glyphs[char]
            start = round(x) + left
            for row, line in enumerate(bitmap):
                for column, alpha in enumerate(line):
                    if alpha:
                        self.blend(start + column, baseline + top + row, colour, alpha)
            x += advance
        return x


def window(daily: Sequence[Bar], span: Span) -> list[Bar]:
    months, _, _, by_week = SPANS[span]
    last = daily[-1].day
    month = last.year * 12 + last.month - 1 - months
    start = date(month // 12, month % 12 + 1, min(last.day, 28))
    bars = [bar for bar in daily if bar.day >= start]
    return weekly(bars) if by_week else bars


def nice(low: float, high: float, count: int, least: float) -> list[float]:
    raw = (high - low) / count
    magnitude = 10 ** math.floor(math.log10(raw))
    step = max(least, next(factor * magnitude for factor in ((1, 2, 2.5, 5, 10) if magnitude >= 10 else (1, 2, 5, 10)) if factor * magnitude >= raw))
    return [k * step for k in range(math.ceil(low / step), math.floor(high / step) + 1) if low < k * step < high]


def decimals(price: float) -> int:
    return 2 if price < 50 else 1 if price < 500 else 0


def lots(volume: float) -> str:
    count = volume / 1000
    return f"{count / 1e4:g}萬" if count >= 1e4 else f"{count:,.0f}"


def colour(bar: Bar) -> Colour:
    return UP if bar.close > bar.open else DOWN if bar.close < bar.open else FLAT


def render(daily: Sequence[Bar], span: Span) -> bytes:
    bars = window(daily, span)
    _, period, reach, by_week = SPANS[span]
    last = daily[-1]
    previous = daily[-2].close if len(daily) > 1 else None
    canvas = Canvas()
    count = len(bars)
    high, low = max(bar.high for bar in bars), min(bar.low for bar in bars)
    places = decimals(high)
    if high == low:
        pad = max(high * 0.02, 10**-places)
        high, low = high + pad, low - pad
    per_pixel = (high - low) / (K_BOTTOM - K_TOP - 2 * LABEL_ROOM)
    top, bottom = high + per_pixel * LABEL_ROOM, low - per_pixel * LABEL_ROOM
    right = W - max(GUTTER, math.ceil(width(f"{top:,.{places}f}", "sb")) + 24)
    step = (right - LEFT) / count
    body = max(1, min(int(step * 0.68), MAX_BODY)) | 1
    centre = [round(LEFT + step * (i + 0.5)) for i in range(count)]
    peak = max(bar.volume for bar in bars)
    scale = peak * 1.1 or 1

    def py(price: float) -> int:
        return round(K_TOP + (top - price) / (top - bottom) * (K_BOTTOM - K_TOP))

    def vy(volume: float) -> int:
        return round(V_BOTTOM - volume / scale * (V_BOTTOM - V_TOP))

    for price in nice(bottom, top, 5, 10**-places):
        canvas.dashes(LEFT, right, py(price), GRID)
        if abs(py(price) - py(last.close)) > 18:
            canvas.text(right + 8, py(price), f"{price:,.{places}f}", MUTED, anchor="lm")
    for volume in nice(0, scale, 2, 1000):
        canvas.dashes(LEFT, right, vy(volume), GRID)
        canvas.text(right + 8, vy(volume), lots(volume), MUTED, anchor="lm")
    canvas.text(LEFT + 2, V_TOP - 3, "成交量 (張)", MUTED, anchor="lt")

    week_ticks = not by_week and count <= 30
    for i in range(1, count):
        day, prior = bars[i].day, bars[i - 1].day
        turn = day.isocalendar()[1] != prior.isocalendar()[1] if week_ticks else day.month != prior.month
        if turn and (not by_week or day.month % 2 == 1):
            label = f"{day.month}/{day.day}" if week_ticks else f"{day.year}年{day.month}月" if by_week or day.month == 1 else f"{day.month}月"
            canvas.vdashes(centre[i], K_TOP, K_BOTTOM, GRID)
            canvas.vdashes(centre[i], V_TOP, V_BOTTOM, GRID)
            half = width(label) / 2
            canvas.text(min(max(centre[i], LEFT + half), right - half), V_BOTTOM + 8, label, MUTED, anchor="mt")
    canvas.rect(LEFT, K_BOTTOM, right, K_BOTTOM + 1, AXIS)
    canvas.rect(LEFT, V_BOTTOM, right, V_BOTTOM + 1, AXIS)
    canvas.rect(right, K_TOP, right + 1, V_BOTTOM + 1, AXIS)

    for i, bar in enumerate(bars):
        shade = colour(bar)
        left = centre[i] - body // 2
        upper = py(max(bar.open, bar.close))
        canvas.rect(centre[i], py(bar.high), centre[i] + 1, py(bar.low) + 1, shade)
        canvas.rect(left, upper, left + body, max(py(min(bar.open, bar.close)), upper + 1), shade)
        canvas.rect(left, vy(bar.volume), left + body, V_BOTTOM, DIM[shade])

    cap = ATLAS["s"][0]
    for name, above in (("最高", True), ("最低", False)):
        i = max(range(count), key=lambda n: bars[n].high if above else -bars[n].low)
        value = bars[i].high if above else bars[i].low
        label = f"{name} {value:,.{places}f}"
        wide = math.ceil(width(label))
        x = min(max(centre[i] - wide // 2, LEFT + 4), right - 6 - wide)
        y = py(value)
        box = (x - 3, y - cap - 12, x + wide + 3, y - 3) if above else (x - 3, y + 4, x + wide + 3, y + cap + 13)
        canvas.rect(*box, BG)
        canvas.text(x, box[1] + 5, label, TEXT, anchor="lt")

    change = 0.0 if previous is None else last.close - previous
    shade = UP if change > 0 else DOWN if change < 0 else FLAT
    y = py(last.close)
    canvas.dashes(LEFT, right, y, shade, 2, 3)
    canvas.rounded(right + 1, y - 10, W - 8, y + 10, shade, 3)
    canvas.text(right + 8, y, f"{last.close:,.{places}f}", WHITE, "sb", "lm")

    x = canvas.text(16, 40, period, WHITE, "xl")
    canvas.text(x + 10, 40, reach, MUTED)
    edge = W - 16
    if previous is not None:
        arrow = "▲ " if change > 0 else "▼ " if change < 0 else ""
        tail = f"{arrow}{abs(change):,.2f} ({change / previous * 100:+.2f}%)"
        canvas.text(edge, 40, tail, shade, "m", "rs")
        edge -= 8 + width(tail, "m")
    canvas.text(edge, 40, f"{last.close:,.2f}", shade, "xl", "rs")
    x = W - 16
    fields = [last.day.strftime("%Y/%m/%d")] + [f"{name} {value:,.{places}f}" for name, value in (("開", last.open), ("高", last.high), ("低", last.low), ("收", last.close))]
    for field in reversed([*fields, f"量 {last.volume / 1000:,.0f} 張"]):
        canvas.text(x, 60, field, MUTED, anchor="rs")
        x -= width(field) + 12
    return png(W, H, [bytes(row) for row in canvas.rows], 2)

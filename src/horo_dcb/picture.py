import math
import struct
import zlib
from collections.abc import Sequence
from pathlib import Path

from .blackjack import RANKS, SUITS, Card
from .slots import STRIP, Stops

ASSETS = Path(__file__).parent / "assets"
CARD_W, CARD_H, CARD_GAP, CARD_CORNER, HAND_SLOTS, HAND_PAD = 88, 124, 10, 12, 5, 4
BACK = len(SUITS) * len(RANKS)
CELL, PAD, GUTTER, RADIUS = 92, 14, 8, 12
COLOURS = 128
PANEL, WELL, GREEN = COLOURS - 3, COLOURS - 2, COLOURS - 1
CLEAR, END = COLOURS, COLOURS + 1
SEGMENT = 100
FPS = 25
STOPS_AT = (1.0, 1.55, 2.1)
SETTLE, SPEED, BLUR_SPEED, BOUNCE, BOUNCE_TIME = 0.45, 1500, 650, 7, 0.2
HOLD = 6000


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def png(width: int, height: int, rows: Sequence[bytes], colour_type: int, palette: bytes = b"") -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, colour_type, 0, 0, 0)
    body = b"".join(b"\x00" + row for row in rows)
    plte = _chunk(b"PLTE", palette) if palette else b""
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header) + plte + _chunk(b"IDAT", zlib.compress(body, 6)) + _chunk(b"IEND", b"")


def card_index(card: Card | None) -> int:
    return BACK if card is None else SUITS.index(card.suit) * len(RANKS) + RANKS.index(card.rank)


class Deck:
    def __init__(self, path: Path = ASSETS / "cards.bin") -> None:
        raw = zlib.decompress(path.read_bytes())
        stride, size = CARD_W * 4, CARD_W * CARD_H * 4
        self.faces = [[raw[face * size + row * stride : face * size + (row + 1) * stride] for row in range(CARD_H)] for face in range(BACK + 1)]

    def hand(self, cards: Sequence[Card | None]) -> bytes:
        width = HAND_SLOTS * CARD_W + (HAND_SLOTS - 1) * CARD_GAP
        step = CARD_W + CARD_GAP if len(cards) <= HAND_SLOTS else (width - CARD_W) // (len(cards) - 1)
        faces = [self.faces[card_index(card)] for card in cards]
        blank = bytes(width * 4)
        rows = [blank] * HAND_PAD
        for row in range(CARD_H):
            line = bytearray(width * 4)
            for position, face in enumerate(faces):
                start, source = position * step * 4, face[row]
                under = bytes(line[start : start + CARD_CORNER * 4])
                line[start : start + len(source)] = source
                if position and step < CARD_W:
                    for pixel in range(0, CARD_CORNER * 4, 4):
                        alpha = source[pixel + 3]
                        if alpha < 255:
                            for channel in range(3):
                                line[start + pixel + channel] = (source[pixel + channel] * alpha + under[pixel + channel] * (255 - alpha)) // 255
                            line[start + pixel + 3] = max(alpha, under[pixel + 3])
            rows.append(bytes(line))
        return png(width, CARD_H + 2 * HAND_PAD, rows + [blank] * HAND_PAD, 6)


def _lzw(pixels: bytes) -> bytes:
    out = bytearray([CLEAR])
    table: dict[int, int] = {}
    code = END + 1
    prefix = pixels[0]
    for pixel in pixels[1:]:
        key = prefix << 8 | pixel
        found = table.get(key)
        if found is None:
            out.append(prefix)
            table[key] = code
            code += 1
            prefix = pixel
        else:
            prefix = found
    out.append(prefix)
    return bytes(out)


def _packed(pixels: bytes) -> bytes:
    return b"".join(_lzw(pixels[start : start + SEGMENT]) for start in range(0, len(pixels), SEGMENT))


def _image(left: int, top: int, width: int, height: int, stream: bytes, delay: int) -> bytes:
    data = stream + bytes([END])
    blocks = b"".join(bytes([len(data[start : start + 255])]) + data[start : start + 255] for start in range(0, len(data), 255))
    control = b"\x21\xf9\x04\x04" + struct.pack("<H", delay) + b"\x00\x00"
    return control + b"\x2c" + struct.pack("<HHHH", left, top, width, height) + b"\x00\x07" + blocks + b"\x00"


def _inset(row: int) -> int:
    edge = min(row, CELL * 3 - 1 - row)
    return math.ceil(RADIUS - math.sqrt(RADIUS * RADIUS - (RADIUS - edge - 0.5) ** 2)) if edge < RADIUS else 0


def _position(time: float, end: float) -> tuple[float, bool]:
    if time >= end:
        return -BOUNCE * math.sin(math.pi * min(1.0, (time - end) / BOUNCE_TIME)), False
    if time < end - SETTLE:
        return SPEED * (end - SETTLE - time) + SPEED * SETTLE / 2, True
    remaining = end - time
    return SPEED * remaining * remaining / (2 * SETTLE), SPEED * remaining / SETTLE > BLUR_SPEED


class Reel:
    def __init__(self, path: Path = ASSETS / "reel.bin") -> None:
        raw = zlib.decompress(path.read_bytes())
        self.palette = raw[: COLOURS * 3]
        height = CELL * len(STRIP)
        pixels = raw[COLOURS * 3 :]
        self.height = height
        self.strips = {blur: [pixels[offset + row * CELL : offset + (row + 1) * CELL] for row in range(height)] for blur, offset in ((False, 0), (True, height * CELL))}
        self.packed = {(blur, led): [_lzw(bytes([PANEL]) * GUTTER * led + row) for row in rows] for blur, rows in self.strips.items() for led in (False, True)}
        self.insets = [_inset(row) for row in range(CELL * 3)]
        self.size = (PAD * 2 + CELL * 3 + GUTTER * 2, PAD * 2 + CELL * 3)

    def _window(self, blur: bool, top: int, row: int) -> bytes:
        line = self.strips[blur][(top + row) % self.height]
        inset = self.insets[row]
        return bytes([PANEL]) * inset + line[inset : CELL - inset] + bytes([PANEL]) * inset if inset else line

    def _segment(self, blur: bool, top: int, row: int, led: bool) -> bytes:
        if self.insets[row]:
            return _packed(bytes([PANEL]) * GUTTER * led + self._window(blur, top, row))
        return self.packed[blur, led][(top + row) % self.height]

    def board(self, stops: Stops, rows: Sequence[int] = ()) -> list[bytes]:
        width, height = self.size
        canvas = [bytearray([PANEL]) * width for _ in range(height)]
        for row in range(CELL * 3):
            for column, stop in enumerate(stops):
                left = PAD + column * (CELL + GUTTER)
                canvas[PAD + row][left : left + CELL] = self._window(False, stop * CELL, row)
        for line in rows:
            top, bottom, left, right = PAD + line * CELL + 2, PAD + (line + 1) * CELL - 3, PAD - 6, width - PAD + 5
            for y in range(top, bottom + 1):
                edge = min(y - top, bottom - y)
                if edge < 4:
                    inset = 5 - edge
                    canvas[y][left + inset : right + 1 - inset] = bytes([GREEN]) * (right + 1 - left - 2 * inset)
                else:
                    canvas[y][left : left + 4] = bytes([GREEN]) * 4
                    canvas[y][right - 3 : right + 1] = bytes([GREEN]) * 4
        return [bytes(row) for row in canvas]

    def still(self, stops: Stops, rows: Sequence[int] = ()) -> bytes:
        width, height = self.size
        return png(width, height, self.board(stops, rows), 3, self.palette)

    def frames(self, stops: Stops) -> list[list[tuple[bool, int, bool]]]:
        frames = []
        for index in range(int((STOPS_AT[-1] + BOUNCE_TIME) * FPS) + 1):
            time = index / FPS
            columns = []
            for stop, end in zip(stops, STOPS_AT, strict=True):
                offset, blur = _position(time, end)
                columns.append((blur, (stop * CELL + round(offset)) % self.height, time >= end + BOUNCE_TIME))
            frames.append(columns)
        return frames

    def spin(self, stops: Stops, rows: Sequence[int] = ()) -> bytes:
        width, height = self.size
        out = bytearray(b"GIF89a" + struct.pack("<HHBBB", width, height, 0xF6, PANEL, 0) + self.palette)
        frames = self.frames(stops)
        delay = round(100 / FPS)
        edge, side = _packed(bytes([PANEL]) * width), _packed(bytes([PANEL]) * PAD)
        for number, columns in enumerate(frames):
            first = 0 if number == 0 else next((column for column in range(3) if not frames[number - 1][column][2]), 2)
            stream = bytearray(edge * PAD if number == 0 else b"")
            for row in range(CELL * 3):
                if number == 0:
                    stream += side
                for column in range(first, 3):
                    blur, top, _ = columns[column]
                    stream += self._segment(blur, top, row, column > first)
                if number == 0:
                    stream += side
            if number == 0:
                out += _image(0, 0, width, height, bytes(stream + edge * PAD), delay)
            else:
                left = PAD + first * (CELL + GUTTER)
                out += _image(left, PAD, width - PAD - left, CELL * 3, bytes(stream), delay)
        out += _image(0, 0, width, height, b"".join(map(_packed, self.board(stops, rows))), HOLD)
        return bytes(out) + b"\x3b"

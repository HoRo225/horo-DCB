import json
import struct
import zlib
from datetime import date, timedelta
from pathlib import Path

import pytest

from horo_dcb.chart import render, width
from horo_dcb.market import parse_bars
from horo_dcb.portfolio import Bar

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "stocks" / "price_2330.json"
UP, DOWN, FLAT, WHITE = (0xF2, 0x4B, 0x55), (0x2D, 0xBE, 0x7E), (0xB5, 0xBA, 0xC1), (0xFF, 0xFF, 0xFF)
W, H = 800, 480


def decoded(data: bytes) -> list[list[tuple[int, int, int]]]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    at, chunks = 8, {}
    while at < len(data):
        (length,) = struct.unpack_from(">I", data, at)
        kind = data[at + 4 : at + 8]
        chunks[kind] = chunks.get(kind, b"") + data[at + 8 : at + 8 + length]
        at += 12 + length
    width_, height, depth, colour_type = struct.unpack_from(">IIBB", chunks[b"IHDR"])
    assert (width_, height, depth, colour_type) == (W, H, 8, 2)
    raw = zlib.decompress(chunks[b"IDAT"])
    stride = W * 3 + 1
    rows = []
    for y in range(H):
        line = raw[y * stride : (y + 1) * stride]
        assert line[0] == 0
        rows.append([tuple(line[1 + x * 3 : 4 + x * 3]) for x in range(W)])
    return rows


def colours(rows, x0: int, y0: int, x1: int, y1: int) -> set:
    return {rows[y][x] for y in range(y0, y1) for x in range(x0, x1)}


def history() -> list[Bar]:
    return parse_bars(json.loads(FIXTURE.read_text(encoding="utf-8"))["data"])


def days(count: int, **fields) -> list[Bar]:
    start = date(2026, 8, 3)
    return [Bar(**{"day": start + timedelta(days=n + 2 * (n // 5)), "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 2_000_000} | fields) for n in range(count)]


@pytest.mark.parametrize("span", ["1m", "3m", "1y"])
def test_every_span_renders_a_png_with_both_candle_colours(span):
    rows = decoded(render(history(), span))
    chart = colours(rows, 14, 78, 730, 330)
    assert UP in chart and DOWN in chart


def test_title_shows_the_drop_against_the_previous_close():
    rows = decoded(render(history(), "3m"))
    title = colours(rows, 400, 10, 790, 45)
    assert DOWN in title and UP not in title


def test_title_shows_a_rise_in_the_up_colour():
    bars = history()
    rising = [*bars[:-1], Bar(bars[-1].day, 2000.0, 2200.0, 1990.0, 2150.0, 1000)]
    title = colours(decoded(render(rising, "1m")), 400, 10, 790, 45)
    assert UP in title and DOWN not in title


@pytest.mark.parametrize("span", ["1m", "1y"])
def test_a_single_bar_renders_without_a_change(span):
    rows = decoded(render(days(1), span))
    title = colours(rows, 400, 10, 790, 45)
    assert FLAT in title and UP not in title and DOWN not in title


def test_unchanging_prices_render():
    rows = decoded(render(days(20, open=50.0, high=50.0, low=50.0, close=50.0), "1m"))
    assert FLAT in colours(rows, 14, 78, 730, 330)


def test_days_without_volume_render():
    rows = decoded(render(days(40, volume=0), "3m"))
    assert UP in colours(rows, 14, 78, 730, 330)


@pytest.mark.parametrize("price", [12345.0, 1234567.0])
def test_large_prices_stay_inside_the_price_tag(price):
    rows = decoded(render(days(30, open=price * 0.99, high=price * 1.01, low=price * 0.98, close=price), "1m"))
    tag = [x for y in range(70, 450) for x in range(600, W) if rows[y][x] == WHITE]
    assert tag
    assert max(tag) < W - 8


@pytest.mark.parametrize(
    ("label", "face"),
    [
        ("近1個月", "s"),
        ("近3個月", "s"),
        ("近1年", "s"),
        ("成交量 (張)", "s"),
        ("最高 2,590.55", "s"),
        ("最低 9.85", "s"),
        ("2026年11月", "s"),
        ("9月", "s"),
        ("11/20", "s"),
        ("2026/11/20", "s"),
        ("開 2,035 高 2,060 低 1,970 收 2,000", "s"),
        ("量 88,179 張", "s"),
        ("12.5萬", "s"),
        ("日K", "xl"),
        ("週K", "xl"),
        ("12,345.67", "xl"),
        ("▲ 100.00 (+4.76%)", "m"),
        ("▼ 0.05 (-0.51%)", "m"),
        ("1,234,567.89", "sb"),
    ],
)
def test_every_drawn_label_has_glyphs(label, face):
    assert width(label, face) > 0


def test_a_missing_glyph_is_an_error():
    with pytest.raises(KeyError):
        width("龜")

import asyncio
import io
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import discord
from PIL import Image

from src import cards

PNG = b"\x89PNG\r\n\x1a\n"


def canvas() -> Image.Image:
    return Image.new("RGBA", (960, 200), (30, 31, 34, 255))


def red_png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (256, 256), "red").save(buf, "PNG")
    return buf.getvalue()


def opened(file: discord.File) -> Image.Image:
    return Image.open(file.fp).convert("RGBA")


def fake_asset(read) -> MagicMock:
    asset = MagicMock()
    asset.replace.return_value = asset
    asset.url = "https://cdn.example/a.png"
    asset.read = read
    return asset


class TextTests(unittest.TestCase):
    def test_mixed_names_draw(self):
        for name in ("HoRo 樂奈", "😀 HoRo", "▍ᴀɪꜱ", "한글", ""):
            with self.subTest(name=name):
                for bold in (False, True):
                    width = cards.draw_text(canvas(), (10, 10), name, 40, "white", bold=bold)
                    self.assertGreaterEqual(width, 0)

    def test_centre_anchor_keeps_width(self):
        img = canvas()
        self.assertEqual(
            cards.draw_text(img, (480, 100), "生日快樂 HoRo", 50, "white", bold=True, anchor="mm"),
            cards.draw_text(canvas(), (0, 0), "生日快樂 HoRo", 50, "white", bold=True),
        )
        self.assertIsNotNone(img.getbbox())

    @unittest.skipUnless(cards.DEJAVU.exists(), "DejaVu Sans not installed")
    def test_small_caps_fall_back_to_dejavu(self):
        self.assertGreater(cards.draw_text(canvas(), (0, 0), "ᴀɪꜱ", 40, "white"), 0)

    @unittest.skipUnless(cards.EMOJI_FONT.exists(), "Noto Color Emoji not installed")
    def test_emoji_drawn_in_colour(self):
        img = canvas()
        self.assertGreater(cards.draw_text(img, (0, 0), "😀", 40, "white"), 0)
        self.assertTrue(any(max(px[:3]) - min(px[:3]) > 80 for px in img.crop((0, 0, 60, 80)).getdata()))

    def test_missing_emoji_font_skips_glyph(self):
        chain = (cards.TC_FONT, Path("/missing/emoji.ttf"), cards.DEJAVU)
        with patch.object(cards, "CHAIN", chain):
            self.assertEqual(cards.draw_text(canvas(), (0, 0), "🎂", 40, "white"), 0)


class FitTests(unittest.TestCase):
    def test_clip_counts_the_ellipsis(self):
        for text in ("HoRo 樂奈的超級長名字 with English words", "😀🎂 生日快樂 HoRo 😀🎂🎉", f"{10**18:,} 點"):
            for bold in (False, True):
                with self.subTest(text=text, bold=bold):
                    cut = cards.clip(text, 30, 200, bold)
                    self.assertTrue(cut.endswith("…"))
                    self.assertLessEqual(cards.text_width(cut, 30, bold), 200)

    def test_clip_keeps_short_text(self):
        self.assertEqual(cards.clip("HoRo", 30, 200), "HoRo")

    def test_fit_shrinks_before_clipping(self):
        text = f"{10**12:,} 點"
        width = cards.text_width(text, 60, True)
        self.assertEqual(cards.fit(text, 96, width, True), (text, 60))
        cut, size = cards.fit(f"{10**18:,} 點", 96, 300, True)
        self.assertEqual(size, 40)
        self.assertTrue(cut.endswith("…"))


class TemplateTests(unittest.TestCase):
    LONG = "超級無敵長的名字😀" * 8

    def assert_inside(self, img: Image.Image, size: tuple[int, int], *gaps: int):
        self.assertEqual(img.size, size)
        for x in (size[0] - 1, *gaps):
            self.assertEqual(img.crop((x, 0, x + 1, size[1])).getcolors(), [(size[1], (*cards.BG, 255))], x)

    def test_t3(self):
        for avatars, gap in (([], 900), ([red_png(), None, b"bad"], 756), ([None] * 120, 630)):
            with self.subTest(count=len(avatars)):
                img = opened(cards.t3(self.LONG, self.LONG, avatars, "rocket", "t3.png"))
                self.assert_inside(img, (960, 240), gap)


class ImageTests(unittest.TestCase):
    def test_to_file_writes_png(self):
        file = cards.to_file(canvas(), "card.png")
        self.assertEqual(file.filename, "card.png")
        self.assertEqual(file.fp.read(8), PNG)

    def test_avatar_circle_size_and_smooth_edge(self):
        for data in (red_png(), None, b"not an image", b"", cards.icon("game_die", 64)):
            with self.subTest(data=type(data).__name__):
                face = cards.avatar_circle(data, 184)
                self.assertEqual(face.size, (184, 184))
                self.assertTrue(any(0 < a < 255 for _, a in face.getchannel("A").getcolors()))

    def test_icon_size_and_copy(self):
        first = cards.icon("game_die", 64)
        self.assertEqual(first.size, (64, 64))
        first.putalpha(0)
        self.assertIsNotNone(cards.icon("game_die", 64).getbbox())


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        cards._avatars.clear()

    async def test_fetch_avatar_failures_return_none(self):
        errors = (
            discord.HTTPException(MagicMock(status=500, reason="x"), "boom"),
            aiohttp.ClientError(),
            TimeoutError(),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__):
                self.assertIsNone(await cards.fetch_avatar(fake_asset(AsyncMock(side_effect=error))))

    async def test_fetch_avatar_times_out(self):
        async def slow():
            await asyncio.sleep(10)

        timeout = asyncio.timeout
        with patch("src.cards.asyncio.timeout", lambda _: timeout(0.01)):
            self.assertIsNone(await cards.fetch_avatar(fake_asset(slow)))

    async def test_fetch_avatar_caches_by_url(self):
        read = AsyncMock(return_value=PNG)
        asset = fake_asset(read)
        self.assertEqual(await cards.fetch_avatar(asset), PNG)
        self.assertEqual(await cards.fetch_avatar(asset), PNG)
        read.assert_awaited_once()
        asset.replace.assert_called_with(size=256, format="png")

    async def test_render_uses_card_thread(self):
        name = await cards.render(lambda: threading.current_thread().name)
        self.assertTrue(name.startswith("cards"))


if __name__ == "__main__":
    unittest.main()

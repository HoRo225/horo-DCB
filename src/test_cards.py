import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from src import cards


def canvas() -> Image.Image:
    return Image.new("RGBA", (960, 200), (30, 31, 34, 255))


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


class ImageTests(unittest.TestCase):
    def test_icon_size_and_copy(self):
        first = cards.icon("game_die", 64)
        self.assertEqual(first.size, (64, 64))
        first.putalpha(0)
        self.assertIsNotNone(cards.icon("game_die", 64).getbbox())


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_render_uses_card_thread(self):
        name = await cards.render(lambda: threading.current_thread().name)
        self.assertTrue(name.startswith("cards"))


if __name__ == "__main__":
    unittest.main()

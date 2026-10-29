import io
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageChops

from src.games.adapters.discord import art
from src.games.domain.catalog import GAMES

CAPTION_BAND = (0, 185, 1200, 300)


def canvas() -> Image.Image:
    return Image.new("RGBA", (960, 200), (30, 31, 34, 255))


def drawn(png: bytes) -> Image.Image:
    with Image.open(io.BytesIO(png)) as image:
        return image.convert("RGB")


def ink(image: Image.Image, band: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    part = image.crop(band)
    left, top, right, bottom = ImageChops.difference(part, Image.new("RGB", part.size, art.BG)).getbbox()
    return band[0] + left, band[1] + top, band[0] + right, band[1] + bottom


class TextTests(unittest.TestCase):
    def test_mixed_names_draw(self):
        for name in ("HoRo 樂奈", "遊戲大廳", "21 點・拉霸", ""):
            with self.subTest(name=name):
                for bold in (False, True):
                    width = art.draw_text(canvas(), (10, 10), name, 40, "white", bold=bold)
                    self.assertGreaterEqual(width, 0)

    def test_centre_anchor_keeps_width(self):
        img = canvas()
        self.assertEqual(
            art.draw_text(img, (480, 100), "生日快樂 HoRo", 50, "white", bold=True, anchor="mm"),
            art.draw_text(canvas(), (0, 0), "生日快樂 HoRo", 50, "white", bold=True),
        )
        self.assertIsNotNone(img.getbbox())


class ImageTests(unittest.TestCase):
    def test_assets_stay_under_src(self):
        self.assertEqual(art.ASSETS, Path(art.__file__).parents[3] / "assets")
        self.assertTrue(art.TC_FONT.exists())

    def test_hub_banner_is_a_wide_png(self):
        with Image.open(io.BytesIO(art.banner_png())) as banner:
            self.assertEqual((banner.format, banner.size), ("PNG", (1200, 300)))
        self.assertEqual(art.banner_file().filename, "hub.png")

    def test_hub_banner_icons_sit_level_with_the_title(self):
        banner = drawn(art.banner_png())
        title = ink(banner, (390, 0, 810, 185))
        for band in ((0, 0, 380, 300), (820, 0, 1200, 300)):
            with self.subTest(band):
                icons = ink(banner, band)
                self.assertAlmostEqual((icons[1] + icons[3]) / 2, (title[1] + title[3]) / 2, delta=1)

    def test_hub_banner_caption_lists_the_catalog(self):
        both = ink(drawn(art.banner_png()), CAPTION_BAND)
        with patch.object(art, "GAMES", {"blackjack": GAMES["blackjack"]}):
            one = ink(drawn(art.banner_png()), CAPTION_BAND)
        self.assertGreater(both[2] - both[0], one[2] - one[0])
        self.assertAlmostEqual(both[0] + both[2], 1200, delta=2)
        self.assertAlmostEqual(one[0] + one[2], 1200, delta=2)

    def test_icon_size_and_copy(self):
        first = art.icon("game_die", 64)
        self.assertEqual(first.size, (64, 64))
        first.putalpha(0)
        self.assertIsNotNone(art.icon("game_die", 64).getbbox())


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_render_uses_card_thread(self):
        name = await art.render(lambda: threading.current_thread().name)
        self.assertTrue(name.startswith("cards"))


if __name__ == "__main__":
    unittest.main()

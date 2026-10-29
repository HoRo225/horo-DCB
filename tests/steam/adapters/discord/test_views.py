import unittest
from types import SimpleNamespace

import discord

from src.core.adapters.discord.brand import BRAND_COLOUR, ICONS
from src.steam.adapters.discord.views import build_offer_view, offer_summary
from tests.support.steam import offer
from tests.support.testing import text

DATED = offer(1, name="**g**", header_image="https://cdn.example.com/h.jpg", ends_at=1792083600)


def role(role_id: int) -> SimpleNamespace:
    return SimpleNamespace(id=role_id, mention=f"<@&{role_id}>")


class OfferViewTest(unittest.TestCase):
    def test_card_is_banner_title_small_line_blurb_and_store_link(self) -> None:
        card = build_offer_view(DATED, [role(1)])
        self.assertEqual(card.children[0].content, "<@&1>")
        self.assertEqual(text(card).split("\n")[1:], [
            rf"## {ICONS['steam']} \*\*g\*\*", "-# 限時免費 · ~~NT$100~~ · <t:1792083600:f> 截止（<t:1792083600:R>）", "desc",
        ])
        self.assertIsInstance(card.children[1].children[0], discord.ui.MediaGallery)
        self.assertEqual(card.children[1].accent_colour, BRAND_COLOUR)
        buttons = [item for item in card.walk_children() if isinstance(item, discord.ui.Button)]
        self.assertEqual([(b.label, b.url) for b in buttons], [("前往 Steam 領取", "https://store.steampowered.com/app/1/")])
        self.assertEqual(card.children[-1].children, buttons)

    def test_deadline_shows_only_when_known(self) -> None:
        self.assertIn("⏰ <t:1792083600:f> 截止（<t:1792083600:R>）", offer_summary(DATED).children[0].content)
        self.assertIn("<t:1792083600:f> 截止（<t:1792083600:R>）", text(build_offer_view(DATED)))
        self.assertNotIn("截止", offer_summary(offer(1)).content)
        self.assertNotIn("截止", text(build_offer_view(offer(1))))

    def test_missing_description_uses_the_default_blurb(self) -> None:
        self.assertIn("Steam 正在進行限時 100% 折扣，可免費加入收藏庫。", text(build_offer_view(offer(1, description=""))))

    def test_long_description_is_shortened_to_fit(self) -> None:
        card = build_offer_view(offer(1, description="說" * 5000, header_image="https://cdn.example.com/h.jpg"))
        self.assertLessEqual(card.content_length(), 4000)
        self.assertTrue(text(card).endswith("說明已截短，完整內容請前往 Steam 查看。"))


if __name__ == "__main__":
    unittest.main()

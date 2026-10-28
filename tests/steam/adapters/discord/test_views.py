import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from src.core.adapters.discord.brand import BRAND_COLOUR, ICONS
from src.core.app.supervisor import SCHEDULED, TaskGroup
from src.steam import Steam
from src.steam.adapters.discord.views import build_offer_view, offer_summary, steam_claim_item
from tests.support.steam import FakeAnnouncer, FakeGuild, FakeSource, FakeStore, announcement_effect, offer
from tests.support.testing import text

G = 100
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


class SteamClaimTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        store = FakeStore()
        store.add(announcement_effect("done", 7, status="done", role_ids=(1, 2), message_id=555))
        self.claim = steam_claim_item(Steam(store, FakeSource(), FakeAnnouncer(FakeGuild()), TaskGroup("steam", SCHEDULED)))
        self.guild = SimpleNamespace(get_role={1: role(1)}.get)

    async def press(self, message_id: int) -> tuple[list[str], MagicMock]:
        interaction = MagicMock(guild_id=G, user=SimpleNamespace(id=2))
        interaction.client.get_guild = {G: self.guild}.get
        interaction.message = SimpleNamespace(id=message_id)
        response = interaction.response
        response.edit_message, response.send_message, interaction.followup.send = AsyncMock(), AsyncMock(), AsyncMock()
        response.is_done.side_effect = lambda: response.edit_message.await_count > 0
        match = self.claim.__discord_ui_compiled_template__.fullmatch("horo:steam:claim:7")
        item = await self.claim.from_custom_id(interaction, None, match)
        await item.callback(interaction)
        private = [*response.send_message.await_args_list, *interaction.followup.send.await_args_list]
        return [text(call.kwargs["view"]) for call in private], interaction

    async def test_old_claim_button_redraws_its_card_without_pinging(self) -> None:
        replies, interaction = await self.press(555)
        self.assertEqual(replies, [f"{ICONS['info']} 這個按鈕已停用，請直接按「前往 Steam 領取」。"])
        edit = interaction.response.edit_message.await_args.kwargs
        self.assertEqual(edit["allowed_mentions"].to_dict(), discord.AllowedMentions.none().to_dict())
        self.assertNotIn("horo:steam:claim:7", str(edit["view"].to_components()))
        self.assertTrue(text(edit["view"]).startswith("<@&1>\n"))

    async def test_unknown_message_only_explains(self) -> None:
        replies, interaction = await self.press(1)
        self.assertIn("這個按鈕已停用", replies[0])
        interaction.response.edit_message.assert_not_awaited()

    def test_claim_button_keeps_its_custom_id(self) -> None:
        self.assertEqual(self.claim(7).item.custom_id, "horo:steam:claim:7")


if __name__ == "__main__":
    unittest.main()

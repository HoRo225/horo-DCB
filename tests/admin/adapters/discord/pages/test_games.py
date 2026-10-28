import unittest
from unittest.mock import AsyncMock, MagicMock

import discord

from src.admin.adapters.discord.pages.games import games_state
from src.core.adapters.discord.brand import WARN_COLOUR
from src.games.adapters.sqlite.store import GAMES_CHANNEL_ID, GAMES_HUB_MESSAGE
from tests.support.admin import G, PanelCase, resolved, text_channel
from tests.support.testing import put_setting

ROLES = "選擇可用身分組（清空即所有人可用）"


class GamesPageTests(PanelCase):
    async def test_channel_publishes_hub_and_roles_limit_players(self) -> None:
        self.channels[800] = channel = text_channel(800, self.guild)
        channel.send = AsyncMock(return_value=MagicMock(id=801))
        await self.open_panel()
        await self.go("遊戲大廳")
        shown = self.screen(await self.choose(self.select("選擇遊戲頻道"), resolved(channel)))
        channel.send.assert_awaited_once()
        self.assertEqual((self.admin.games.channel_id, self.admin.games.hub_message), (800, [800, 801]))
        self.assertIn("✅ 已設定遊戲頻道，並已在新頻道重發遊戲大廳。", shown)
        self.assertIn("🟢 狀態正常 · **遊戲頻道 <#800>**", shown)
        self.assertIn("**常駐訊息**　已發布", shown)
        self.assertIn(f"https://discord.com/channels/{G}/800/801",
                      [getattr(item, "url", None) for item in self.view.walk_children()])

        roles = self.select(ROLES)
        outsider = self.role(901)
        outsider.guild = discord.Object(id=G + 1)
        self.assertIn("只能選擇目前伺服器的一般身分組。", self.screen(await self.choose(roles, outsider)))
        shown = self.screen(await self.choose(roles, self.role(900)))
        self.assertEqual(self.admin.games.role_ids, [900])
        self.assertIn("已更新遊戲大廳可用身分組，共 1 個。", shown)
        self.assertIn("**可用身分組**　<@&900>", shown)
        shown = self.screen(await self.choose(roles))
        self.assertEqual(self.admin.games.role_ids, [])
        self.assertIn("已清除可用身分組，所有人都可以使用遊戲大廳。", shown)
        self.assertIn("**可用身分組**　所有人", shown)

    async def test_unusable_game_channel_needs_attention(self) -> None:
        put_setting(self.db, GAMES_CHANNEL_ID, 800)
        shown = self.screen(await self.open_panel())
        self.assertIn("2 個需要處理", shown)
        self.assertIn("🟠 需要處理 · **🎲 遊戲大廳**\n找不到已綁定的遊戲頻道，請在控制台重新選擇", shown)
        shown = self.screen(await self.go("遊戲大廳"))
        self.assertIn("🟠 需要處理 · **遊戲頻道 <#800>**", shown)
        self.assertIn("**⚠️ 需要處理**\n找不到已綁定的遊戲頻道，請在控制台重新選擇。", shown)
        self.assertEqual(self.view.children[0].accent_colour, WARN_COLOUR)
        self.assertFalse(self.button("重發遊戲大廳").disabled)

    async def test_republish_turns_blue_only_when_the_hub_needs_it(self) -> None:
        primary, secondary = discord.ButtonStyle.primary, discord.ButtonStyle.secondary
        self.channels[800] = text_channel(800, self.guild)
        put_setting(self.db, GAMES_CHANNEL_ID, 800)
        await self.open_panel()
        await self.go("遊戲大廳")
        self.assertEqual(self.button("重發遊戲大廳").style, primary)
        put_setting(self.db, GAMES_HUB_MESSAGE, [800, 801])
        await self.click(self.button("重新整理"))
        self.assertEqual(self.button("重發遊戲大廳").style, secondary)
        self.channels.pop(800)
        await self.click(self.button("重新整理"))
        self.assertEqual(self.button("重發遊戲大廳").style, primary)

    async def test_state_without_bot_member_asks_to_refresh(self) -> None:
        put_setting(self.db, GAMES_CHANNEL_ID, 800)
        self.channels[800] = text_channel(800, self.guild)
        await self.open_panel()
        self.guild.me = None
        state = games_state(self.view)
        self.assertEqual(state.label, "需要處理")
        self.assertEqual(state.summary, "目前無法確認 Bot 的伺服器成員狀態，請稍後重新整理。")


if __name__ == "__main__":
    unittest.main()

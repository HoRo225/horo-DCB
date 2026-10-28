import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from src.core.adapters.discord.live import LiveGuild
from src.core.app.ports import Actor
from src.core.app.supervisor import SCHEDULED, TaskGroup
from src.steam import Steam, SteamFetchResult, SteamOffer
from src.steam.adapters.discord.actions import steam_actions
from tests.support.steam import FakeAnnouncer, FakeGuild, FakeSource, FakeStore
from tests.support.testing import text

G = 100
ADMIN = Actor(1, "管理員", "", frozenset(), True)


def spec(group, name: str):
    (found,) = [item for item in group.specs if item.name == name]
    return found


class SteamQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_query_tells_failure_from_no_offers(self) -> None:
        for result, shown in (
            (None, "無法取得"),
            (SteamFetchResult(frozenset(), (), failed_app_count=3), "無法取得"),
            (SteamFetchResult(frozenset(), ()), "沒有符合條件"),
            (SteamFetchResult(frozenset(), (SteamOffer(1, "遊戲", "NT$1", "", (), ""),), failed_app_count=2), "資料不完整：2 款"),
        ):
            with self.subTest(shown=shown):
                steam = SimpleNamespace(fetch_current_offers=AsyncMock(return_value=result))
                query = spec(steam_actions(steam, SimpleNamespace()), "steam_query")
                self.assertTrue(query.admin)
                self.assertIn(shown, text((await query.effect.render(ADMIN))["view"]))


class SteamChannelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.guild = MagicMock(id=G, unavailable=False)
        text_channel = MagicMock(spec=discord.TextChannel)
        text_channel.configure_mock(id=11, type=discord.ChannelType.text, guild=self.guild)
        voice_channel = MagicMock(spec=discord.VoiceChannel)
        voice_channel.configure_mock(id=12, type=discord.ChannelType.voice, guild=self.guild)
        self.guild.get_channel = {11: text_channel, 12: voice_channel}.get
        client = SimpleNamespace(get_guild=lambda _guild_id: self.guild)
        self.store = FakeStore()
        steam = Steam(self.store, FakeSource(), FakeAnnouncer(FakeGuild(10, 11, 12)), TaskGroup("steam", SCHEDULED))
        self.channel = spec(steam_actions(steam, LiveGuild(client, G, lambda: False)), "steam_channel")

    async def bind(self, channel_id: int):
        step = SimpleNamespace(still_current=lambda: True, operation_id=f"interaction:{channel_id}")
        return await self.channel.effect.run(step, {"channel_id": channel_id})

    async def test_text_channel_is_bound(self) -> None:
        self.assertIsNone(await self.bind(11))
        self.assertEqual(self.store.settings["channel_id"], 11)

    async def test_other_channels_are_refused_without_saving(self) -> None:
        for channel_id in (12, 13):
            with self.subTest(channel_id=channel_id):
                result = await self.bind(channel_id)
                self.assertEqual((result["ok"], result["text"]), (False, "只能選擇目前伺服器的文字頻道。"))
                self.assertEqual(self.store.settings["channel_id"], 10)


if __name__ == "__main__":
    unittest.main()

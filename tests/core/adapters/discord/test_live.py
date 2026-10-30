import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from src.core.adapters.discord.live import LiveGuild, can_send, name_text
from tests.support.discord import Client, Guild, channel, member, thread

G, OTHER, USER, CHANNEL = 5, 6, 1, 10
RESTARTING = "Bot 正在重新啟動，請稍後再試。"
OTHER_GUILD = "此 Bot 只在指定的伺服器提供服務。"
SENDABLE = {"view_channel": True, "send_messages": True}


class LiveGuildTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.closing = False
        self.guild = Guild(G).add(member(USER, roles=(7,), name="玩家"), channel(CHANNEL, **SENDABLE))
        self.client = Client(self.guild)
        self.live = LiveGuild(self.client, G, lambda: self.closing)

    def test_unavailable_reason_names_restart_before_wrong_guild(self) -> None:
        self.assertIsNone(self.live.unavailable_reason(G))
        self.assertEqual(self.live.unavailable_reason(OTHER), OTHER_GUILD)
        self.assertEqual(self.live.unavailable_reason(None), OTHER_GUILD)
        self.closing = True
        self.assertEqual(self.live.unavailable_reason(OTHER), RESTARTING)

    def test_only_the_configured_available_guild_is_live(self) -> None:
        self.assertIs(self.live.guild(), self.guild)
        self.guild.unavailable = True
        self.assertIsNone(self.live.guild())
        self.assertIsNone(self.live.member(USER))
        self.assertIsNone(self.live.channel(CHANNEL))
        self.client.guilds = {}
        self.assertIsNone(self.live.guild())
        self.client.guilds = {G: Guild(OTHER)}
        self.assertIsNone(self.live.guild())

    def test_bot_name_is_the_server_nickname_or_the_account_name(self) -> None:
        self.guild.me = SimpleNamespace(display_name="霍蘿")
        self.client.user.name = "HoRo"
        self.assertEqual(self.live.bot_name(), "霍蘿")
        self.guild.me = None
        self.assertEqual(self.live.bot_name(), "HoRo")
        self.client.guilds = {}
        self.assertEqual(self.live.bot_name(), "HoRo")

    def test_name_text_spaces_a_latin_name_only_against_words(self) -> None:
        self.assertEqual(name_text("{name}能幫你做的事", "HoRo"), "HoRo 能幫你做的事")
        self.assertEqual(name_text("{name}能幫你做的事", "樂奈"), "樂奈能幫你做的事")
        self.assertEqual(name_text("比{name}更接近；{name}也是", "HoRo"), "比 HoRo 更接近；HoRo 也是")
        self.assertEqual(name_text("「{name}」 @{name} {name}。", "HoRo"), "「HoRo」 @HoRo HoRo。")
        self.assertEqual(name_text("沒有名字", "HoRo"), "沒有名字")

    def test_admin_follows_the_cached_member(self) -> None:
        self.assertFalse(self.live.admin(USER))
        self.guild.add(member(2, admin=True))
        self.assertTrue(self.live.admin(2))
        self.assertFalse(self.live.admin(3))

    def test_can_send_requires_view_and_the_matching_send_permission(self) -> None:
        user = self.guild.members[USER]
        self.assertTrue(can_send(self.live.channel(CHANNEL), user))
        self.guild.add(channel(12, view_channel=True))
        self.assertFalse(can_send(self.live.channel(12), user))
        self.guild.add(thread(13, CHANNEL, **SENDABLE))
        self.assertFalse(can_send(self.live.channel(13), user))
        self.guild.add(thread(14, CHANNEL, view_channel=True, send_messages_in_threads=True))
        self.assertTrue(can_send(self.live.channel(14), user))
        orphan = thread(15, 99)
        orphan.permissions_for.side_effect = discord.ClientException("parent missing")
        self.guild.add(orphan)
        self.assertFalse(can_send(self.live.channel(15), user))

    async def test_find_member_prefers_the_cache_and_fails_closed(self) -> None:
        self.assertIs(await self.live.find_member(USER), self.guild.members[USER])
        self.guild.fetch_member.assert_not_awaited()
        fetched = member(2)
        self.guild.add(fetched, fetched=True)
        self.assertIs(await self.live.find_member(2), fetched)
        self.assertIsNone(await self.live.find_member(3))

        async def slow(_user_id: int) -> object:
            await asyncio.sleep(1)

        self.guild.fetch_member = AsyncMock(side_effect=slow)
        self.assertIsNone(await self.live.find_member(4, timeout=0.01))

        async def reconnect(_user_id: int) -> object:
            self.client.guilds = {G: Guild(G)}
            return fetched

        self.guild.fetch_member = AsyncMock(side_effect=reconnect)
        self.assertIsNone(await self.live.find_member(2))


if __name__ == "__main__":
    unittest.main()

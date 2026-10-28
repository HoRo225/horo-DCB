import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from src.core.adapters.discord.live import LiveGuild
from src.core.app.ports import Actor, Source
from src.core.domain.actions import REGULAR_ROLES, TEXT_CHANNEL, Target
from tests.support.discord import Client, Guild, channel, member, thread

G, OTHER, USER, CHANNEL = 5, 6, 1, 10
RESTARTING = "Bot 正在重新啟動，請稍後再試。"
OTHER_GUILD = "此 Bot 只在指定的伺服器提供服務。"
UNCONFIRMED = "目前無法確認最新的伺服器或頻道狀態，請稍後再試。"
CANNOT_SEND = "你目前無法在這個頻道執行操作。"
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

    def test_admin_and_avatar_follow_the_cached_member(self) -> None:
        self.assertFalse(self.live.admin(USER))
        self.guild.add(member(2, admin=True))
        self.assertTrue(self.live.admin(2))
        self.assertFalse(self.live.admin(3))
        self.assertEqual(self.live.avatar_url(USER), f"https://cdn.example.com/{USER}.png")
        stranger = 175928847299117063
        self.assertEqual(self.live.avatar_url(stranger), "https://cdn.discordapp.com/embed/avatars/2.png")

    def test_actor_describes_the_cached_member_in_a_live_channel(self) -> None:
        self.assertEqual(
            self.live.actor(CHANNEL, USER),
            Actor(USER, "玩家", f"https://cdn.example.com/{USER}.png", frozenset({7}), False),
        )
        self.assertIsNone(self.live.actor(11, USER))
        self.assertIsNone(self.live.actor(CHANNEL, 2))

    async def test_fresh_actor_uses_fetched_roles_and_fails_closed(self) -> None:
        self.guild.add(member(USER, roles=(8,), admin=True, name="玩家"), fetched=True)
        fresh = await self.live.fresh_actor(CHANNEL, USER)
        self.assertEqual((fresh.role_ids, fresh.admin), (frozenset({8}), True))
        self.assertIsNone(await self.live.fresh_actor(CHANNEL, 2))
        self.assertIsNone(await self.live.fresh_actor(11, USER))
        self.client.guilds = {}
        self.assertIsNone(await self.live.fresh_actor(CHANNEL, USER))

    def test_send_problem_requires_view_and_the_matching_send_permission(self) -> None:
        self.assertIsNone(self.live.send_problem(CHANNEL, USER))
        self.assertEqual(self.live.send_problem(11, USER), UNCONFIRMED)
        self.assertEqual(self.live.send_problem(CHANNEL, 2), UNCONFIRMED)
        self.guild.add(channel(12, view_channel=True))
        self.assertEqual(self.live.send_problem(12, USER), CANNOT_SEND)
        self.guild.add(thread(13, CHANNEL, **SENDABLE))
        self.assertEqual(self.live.send_problem(13, USER), CANNOT_SEND)
        self.guild.add(thread(14, CHANNEL, view_channel=True, send_messages_in_threads=True))
        self.assertIsNone(self.live.send_problem(14, USER))
        orphan = thread(15, 99)
        orphan.permissions_for.side_effect = discord.ClientException("parent missing")
        self.guild.add(orphan)
        self.assertEqual(self.live.send_problem(15, USER), CANNOT_SEND)
        self.closing = True
        self.assertEqual(self.live.send_problem(CHANNEL, USER), RESTARTING)
        self.assertEqual(self.live.send_problem(11, USER), UNCONFIRMED)

    async def test_fetch_member_returns_the_member_of_a_stable_guild(self) -> None:
        fetched = member(USER, roles=(8,))
        self.guild.add(fetched, fetched=True)
        self.assertIs(await self.live.fetch_member(USER), fetched)
        self.guild.fetch_member.assert_awaited_once_with(USER)

    async def test_fetch_member_refetches_after_a_reconnect(self) -> None:
        current = Guild(G).add(member(USER, roles=(9,)))
        stale = member(USER, roles=(8,))

        async def reconnect(_user_id: int) -> object:
            self.client.guilds = {G: current}
            return stale

        self.guild.fetch_member = AsyncMock(side_effect=reconnect)
        self.assertIs(await self.live.fetch_member(USER), current.members[USER])

    async def test_fetch_member_fails_closed_when_the_guild_keeps_changing(self) -> None:
        self.client.guilds = {}
        with self.assertRaisesRegex(ValueError, "^目前無法確認最新的伺服器狀態，請稍後再試。$"):
            await self.live.fetch_member(USER)
        guilds = iter([Guild(G).add(member(USER)) for _ in range(4)])
        self.client.get_guild = lambda _guild_id: next(guilds)
        with self.assertRaisesRegex(ValueError, "^伺服器狀態正在更新，請稍後再試。$"):
            await self.live.fetch_member(USER)

    async def test_fetch_member_propagates_http_errors_without_retry(self) -> None:
        error = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "denied")
        self.guild.fetch_member = AsyncMock(side_effect=error)
        with self.assertRaises(discord.Forbidden):
            await self.live.fetch_member(USER)
        self.guild.fetch_member.assert_awaited_once_with(USER)

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


def text_channel(channel_id: int, guild: object, name: str, **allowed: bool) -> MagicMock:
    fake = MagicMock(spec=discord.TextChannel)
    fake.configure_mock(id=channel_id, guild=guild, type=discord.ChannelType.text, name=name)
    fake.permissions_for.return_value = discord.Permissions(**allowed)
    return fake


def role(role_id: int, name: str, *, default: bool = False) -> SimpleNamespace:
    return SimpleNamespace(id=role_id, name=name, is_default=lambda: default)


class ActionPresenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.cached = member(USER, roles=(7,), name="玩家")
        self.guild = MagicMock(spec=discord.Guild, id=G, unavailable=False)
        self.lobby = text_channel(CHANNEL, self.guild, "大廳", **SENDABLE)
        self.hidden = text_channel(11, self.guild, "秘密", send_messages=True)
        self.roles = [role(G, "@everyone", default=True), role(20, "玩家組"), role(21, "管理組"), role(22, "管理組")]
        self.guild.channels, self.guild.roles = [self.lobby, self.hidden], self.roles
        self.guild.get_member.side_effect = lambda user_id: self.cached if user_id == USER else None
        self.guild.get_channel_or_thread.side_effect = {CHANNEL: self.lobby, 11: self.hidden}.get
        self.guild.get_channel.side_effect = {CHANNEL: self.lobby, 11: self.hidden}.get
        self.guild.get_role.side_effect = {item.id: item for item in self.roles}.get
        self.guild.fetch_member = AsyncMock(return_value=self.cached)
        self.closing = False
        self.live = LiveGuild(SimpleNamespace(get_guild=lambda guild_id: self.guild if guild_id == G else None),
                              G, lambda: self.closing)
        self.actor = Actor(USER, "玩家", f"https://cdn.example.com/{USER}.png", frozenset({7}), False)

    async def test_act_problem_checks_the_fetched_member_in_the_live_channel(self) -> None:
        fetched = member(USER, roles=(8,), name="玩家")
        self.guild.fetch_member.return_value = fetched
        self.lobby.permissions_for.side_effect = lambda who: discord.Permissions(
            view_channel=True, send_messages=who is fetched)
        actor = await self.live.fresh_actor(CHANNEL, USER)
        self.assertIsNone(self.live.act_problem(G, CHANNEL, actor))
        self.assertEqual(self.live.act_problem(G, CHANNEL, self.live.actor(CHANNEL, USER)), CANNOT_SEND)
        self.assertEqual(self.live.act_problem(G, 12, actor), UNCONFIRMED)
        self.assertEqual(self.live.act_problem(OTHER, CHANNEL, actor), UNCONFIRMED)
        self.closing = True
        self.assertEqual(self.live.act_problem(G, CHANNEL, actor), RESTARTING)

    def test_target_problem_names_missing_and_unusable_targets(self) -> None:
        voice = Target("voice_channel", ("要語音頻道。", "語音頻道不可用。"))
        cases = (
            (TEXT_CHANNEL, CHANNEL, None, None),
            (TEXT_CHANNEL, 11, "指定頻道不存在或目前無法查看，未執行任何操作。", "指定頻道已不存在或無法查看，未執行這項操作。"),
            (TEXT_CHANNEL, 12, "指定頻道不存在或目前無法查看，未執行任何操作。", "指定頻道已不存在或無法查看，未執行這項操作。"),
            (voice, CHANNEL, "要語音頻道。", "語音頻道不可用。"),
            (Target("text_channels", ("要文字頻道。", "文字頻道不可用。")), [CHANNEL, 11], "要文字頻道。", "文字頻道不可用。"),
            (Target("text_channels", ("要文字頻道。", "文字頻道不可用。")), [12], "指定的頻道或身分組不存在，未執行任何操作。",
             "指定頻道或身分組已不存在，未執行這項操作。"),
            (REGULAR_ROLES, [20], None, None),
            (REGULAR_ROLES, [G], "指定的頻道或身分組不存在，未執行任何操作。", "只能選擇一般身分組，未執行這項操作。"),
            (REGULAR_ROLES, [30], "指定的頻道或身分組不存在，未執行任何操作。", "指定頻道或身分組已不存在，未執行這項操作。"),
            (Target("quoted"), "voice.delete:1", None, None),
        )
        for target, value, preflight, step in cases:
            with self.subTest(target=target.kind, value=value):
                self.assertEqual(self.live.target_problem(target, value, self.actor, preflight=True), preflight)
                self.assertEqual(self.live.target_problem(target, value, self.actor, preflight=False), step)

    def test_mentions_come_from_the_message_and_unique_names_or_ids_in_it(self) -> None:
        source = Source(1000, G, CHANNEL, USER, "給玩家組和 11 還有管理組", frozenset(), frozenset({21}))
        mentions = self.live.mentions(source, self.actor)
        self.assertEqual((mentions.channel_ids, mentions.role_ids), (frozenset({CHANNEL, 11}), frozenset({20, 21})))
        self.assertEqual([(item.id, item.name) for item in mentions.channels], [(CHANNEL, "大廳")])
        self.assertEqual([(item.id, item.name) for item in mentions.roles], [(20, "玩家組"), (21, "管理組")])


if __name__ == "__main__":
    unittest.main()

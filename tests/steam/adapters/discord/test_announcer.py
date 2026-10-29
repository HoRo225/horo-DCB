import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import discord

from src.core.adapters.discord.live import LiveGuild
from src.steam.adapters.discord.announcer import DiscordAnnouncer
from src.steam.app.ports import DeliveryError
from src.steam.domain.offers import PostedCard
from tests.support.steam import offer
from tests.support.testing import text

G, CH, BOT = 100, 10, 99
VIEW_SEND = discord.Permissions(view_channel=True, send_messages=True)


def http_error(status: int) -> discord.HTTPException:
    return discord.HTTPException(SimpleNamespace(status=status, reason="error"), "error")


class DiscordAnnouncerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.guild = MagicMock(id=G, unavailable=False)
        self.guild.me = SimpleNamespace(id=BOT)
        self.channels: dict[int, object] = {}
        self.roles: dict[int, MagicMock] = {}
        self.guild.get_channel = self.channels.get
        self.guild.get_role = self.roles.get
        self.channel = self.text(CH)
        self.client = SimpleNamespace(get_guild=lambda _guild_id: self.guild, wait_until_ready=AsyncMock())
        self.announcer = DiscordAnnouncer(self.client, LiveGuild(self.client, G, lambda: False))

    def text(self, channel_id: int, permissions: discord.Permissions = VIEW_SEND) -> MagicMock:
        channel = MagicMock(spec=discord.TextChannel)
        channel.configure_mock(id=channel_id, type=discord.ChannelType.text, guild=self.guild,
                               send=AsyncMock(return_value=SimpleNamespace(id=555)))
        channel.permissions_for.return_value = permissions
        self.channels[channel_id] = channel
        return channel

    def role(self, role_id: int, *, mentionable: bool = True, default: bool = False) -> MagicMock:
        role = MagicMock(spec=discord.Role)
        role.configure_mock(id=role_id, guild=self.guild, mentionable=mentionable, mention=f"<@&{role_id}>")
        role.is_default.return_value = default
        self.roles[role_id] = role
        return role

    async def test_ready_waits_for_the_client(self) -> None:
        await self.announcer.ready()
        self.client.wait_until_ready.assert_awaited_once()

    def test_guild_is_absent_while_discord_has_none(self) -> None:
        self.guild.unavailable = True
        self.assertIsNone(self.announcer.guild())

    async def test_announcement_mentions_only_the_given_roles(self) -> None:
        first, second = self.role(1), self.role(2)
        message_id = await self.announcer.guild().announce(CH, offer(7), (1, 2), "nonce")
        sent = self.channel.send.await_args.kwargs
        self.assertEqual(message_id, 555)
        self.assertEqual(sent["nonce"], "nonce")
        self.assertEqual(sent["allowed_mentions"].roles, [first, second])
        self.assertFalse(sent["allowed_mentions"].everyone or sent["allowed_mentions"].users)
        self.assertTrue(text(sent["view"]).startswith("<@&1> <@&2>"))

    async def test_announcement_without_roles_mentions_nobody(self) -> None:
        self.channel.send.return_value = SimpleNamespace()
        self.assertIsNone(await self.announcer.guild().announce(CH, offer(7), (), None))
        self.assertIs(self.channel.send.await_args.kwargs["allowed_mentions"].roles, False)

    async def test_delivery_errors_say_whether_discord_rejected_the_message(self) -> None:
        for error, name, rejected in (
            (http_error(403), "HTTPException", True),
            (http_error(429), "HTTPException", False),
            (http_error(503), "HTTPException", False),
            (TimeoutError(), "TimeoutError", False),
            (aiohttp.ClientError(), "ClientError", False),
        ):
            with self.subTest(name=name, rejected=rejected):
                self.channel.send.side_effect = error
                with self.assertRaises(DeliveryError) as caught:
                    await self.announcer.guild().announce(CH, offer(7), (), None)
                self.assertEqual((caught.exception.reason, caught.exception.rejected), (name, rejected))

    async def test_ended_card_replaces_the_posted_message(self) -> None:
        message = SimpleNamespace(edit=AsyncMock())
        self.channel.get_partial_message.return_value = message
        result = await self.announcer.guild().end(PostedCard(CH, 555, offer(7)))
        self.assertEqual(result, "ended")
        self.channel.get_partial_message.assert_called_once_with(555)
        self.assertIn("限免已結束", text(message.edit.await_args.kwargs["view"]))

    async def test_ended_card_gives_up_when_discord_rejects_the_edit(self) -> None:
        message = SimpleNamespace(edit=AsyncMock())
        self.channel.get_partial_message.return_value = message
        guild = self.announcer.guild()
        self.assertEqual(await guild.end(PostedCard(CH + 1, 555, offer(7))), "gone")
        for error, result in (
            (discord.NotFound(SimpleNamespace(status=404, reason="error"), "error"), "gone"),
            (http_error(403), "gone"),
            (http_error(429), "failed"),
            (http_error(503), "failed"),
            (TimeoutError(), "failed"),
            (aiohttp.ClientError(), "failed"),
        ):
            with self.subTest(error=type(error).__name__, result=result):
                message.edit.side_effect = error
                if result == "failed":
                    with self.assertLogs(level="ERROR"):
                        self.assertEqual(await guild.end(PostedCard(CH, 555, offer(7))), result)
                else:
                    self.assertEqual(await guild.end(PostedCard(CH, 555, offer(7))), result)

    def test_role_states(self) -> None:
        self.role(1)
        self.role(2, mentionable=False)
        self.role(G, default=True)
        guild = self.announcer.guild()
        self.assertEqual([guild.role(role_id, CH) for role_id in (1, 2, G, 3)], ["usable", "muted", "default", "missing"])
        self.assertEqual(guild.role(2, None), "muted")
        self.channel.permissions_for.return_value = discord.Permissions(mention_everyone=True)
        self.assertEqual(guild.role(2, CH), "usable")

    def test_channel_problems(self) -> None:
        self.text(11, discord.Permissions.none())
        self.channels[12] = MagicMock(spec=discord.VoiceChannel)
        guild = self.announcer.guild()
        self.assertIsNone(guild.channel_problem(CH))
        self.assertEqual(guild.channel_problem(11), "Bot 在通知頻道缺少權限：檢視頻道、傳送訊息。")
        self.assertEqual(guild.channel_problem(None), "找不到已綁定的通知頻道，請在控制台重新選擇。")
        self.assertIsNone(guild.bind_problem(CH))
        self.assertEqual(guild.bind_problem(11), "Bot 在通知頻道缺少權限：檢視頻道、傳送訊息。")
        self.assertEqual(guild.bind_problem(12), "只能選擇目前伺服器的文字頻道。")
        self.assertEqual(guild.bind_problem(13), "指定通知頻道已不存在，未綁定頻道。")


if __name__ == "__main__":
    unittest.main()

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from src.brand import ICONS
from src.discord_utils import fetch_current_member, hub_link, play_problem, role_problem


class PlayPermissionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.actor = SimpleNamespace(id=1, roles=[SimpleNamespace(id=7)])
        self.guild = MagicMock(spec=discord.Guild, unavailable=False)
        self.guild.get_member.return_value = self.actor
        self.bot = SimpleNamespace(
            guild_id=5,
            games=SimpleNamespace(channel_id=10, role_ids={7}, hub_message=None),
            get_guild=lambda guild_id: self.guild,
            unavailable_reason=lambda guild_id: None,
        )
        self.clicked = MagicMock(spec=discord.Interaction, client=self.bot, guild_id=5, channel_id=10,
                                 user=SimpleNamespace(id=1, roles=[SimpleNamespace(id=7)]))

    def test_interaction_uses_current_roles_instead_of_its_member_snapshot(self):
        self.actor.roles = []
        self.assertEqual(play_problem(self.clicked), "你沒有使用這個功能的身分組。")
        self.clicked.user.roles = []
        self.actor.roles = [SimpleNamespace(id=7)]
        self.assertIsNone(play_problem(self.clicked))

    def test_hub_link_jumps_to_the_published_hub(self):
        self.assertEqual(hub_link(self.bot), [])
        self.bot.games.hub_message = [10, 50]
        [link] = hub_link(self.bot)
        self.assertEqual((link.label, link.url, str(link.emoji)),
                         ("前往遊戲大廳", "https://discord.com/channels/5/10/50", ICONS["games"]))

    def test_role_problem_passes_matching_or_unconfigured_roles(self):
        self.assertIsNone(role_problem(self.bot, self.actor))
        self.assertEqual(role_problem(self.bot, SimpleNamespace(roles=[])), "你沒有使用這個功能的身分組。")
        self.bot.games.role_ids = set()
        self.assertIsNone(role_problem(self.bot, SimpleNamespace(roles=[])))

    def test_missing_member_or_unavailable_guild_fails_closed(self):
        self.guild.get_member.return_value = None
        self.assertIsNotNone(play_problem(self.clicked))
        self.guild.get_member.return_value = self.actor
        self.guild.unavailable = True
        self.assertIsNotNone(play_problem(self.clicked))
        self.guild = None
        self.assertIsNotNone(play_problem(self.clicked))

    def test_domain_context_keeps_its_freshly_fetched_member(self):
        self.actor.roles = []
        context = SimpleNamespace(client=self.bot, guild_id=5, channel_id=10, user=self.clicked.user)
        self.assertIsNone(play_problem(context))


class CurrentMemberTest(unittest.IsolatedAsyncioTestCase):
    async def test_stable_guild_returns_its_fetched_member(self):
        member = object()
        guild = SimpleNamespace(fetch_member=AsyncMock(return_value=member))
        client = SimpleNamespace(get_guild=MagicMock(return_value=guild))
        self.assertEqual(await fetch_current_member(client, 5, 1), (guild, member))
        guild.fetch_member.assert_awaited_once_with(1)

    async def test_reconnect_discards_old_roles_and_fetches_the_new_guild(self):
        old_member, current_member = object(), object()
        current_guild = SimpleNamespace(fetch_member=AsyncMock(return_value=current_member))
        client = SimpleNamespace(get_guild=MagicMock())

        async def reconnect(_user_id):
            client.get_guild.return_value = current_guild
            return old_member

        old_guild = SimpleNamespace(fetch_member=AsyncMock(side_effect=reconnect))
        client.get_guild.return_value = old_guild
        self.assertEqual(await fetch_current_member(client, 5, 1), (current_guild, current_member))
        old_guild.fetch_member.assert_awaited_once_with(1)
        current_guild.fetch_member.assert_awaited_once_with(1)

    async def test_missing_or_repeatedly_replaced_guild_fails_closed(self):
        client = SimpleNamespace(get_guild=MagicMock(return_value=None))
        with self.assertRaisesRegex(ValueError, "無法確認"):
            await fetch_current_member(client, 5, 1)
        guilds = [SimpleNamespace(fetch_member=AsyncMock(return_value=object())) for _ in range(3)]
        client.get_guild.side_effect = [guilds[0], guilds[1], guilds[1], guilds[2]]
        with self.assertRaisesRegex(ValueError, "正在更新"):
            await fetch_current_member(client, 5, 1)
        guilds[0].fetch_member.assert_awaited_once_with(1)
        guilds[1].fetch_member.assert_awaited_once_with(1)
        guilds[2].fetch_member.assert_not_awaited()

    async def test_http_failure_propagates_without_retry(self):
        error = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "denied")
        guild = SimpleNamespace(fetch_member=AsyncMock(side_effect=error))
        client = SimpleNamespace(get_guild=MagicMock(return_value=guild))
        with self.assertRaises(discord.Forbidden) as caught:
            await fetch_current_member(client, 5, 1)
        self.assertIs(caught.exception, error)
        guild.fetch_member.assert_awaited_once_with(1)


if __name__ == "__main__":
    unittest.main()

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from src import testing
from src.ai.access import AiSettings
from src.ai.cog import AiCog
from src.codex.protocol import conversation_key
from src.state import open_db, set_setting


class NewTopicTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.member = SimpleNamespace(id=3, roles=[])
        self.permissions = SimpleNamespace(view_channel=True, send_messages=True, send_messages_in_threads=True, administrator=False)
        self.channel = SimpleNamespace(id=2, permissions_for=lambda member: self.permissions)
        self.guild = MagicMock()
        self.guild.get_member.return_value = self.member
        self.guild.get_channel_or_thread.side_effect = lambda channel_id: self.channel
        self.bot = MagicMock()
        self.bot.unavailable_reason.return_value = None
        self.bot.get_guild.return_value = self.guild
        self.bot.ai_settings.denial_reason.return_value = None
        self.bot.codex.reset_conversation = AsyncMock(return_value=SimpleNamespace(archive_unconfirmed_count=0))
        self.interaction = testing.interaction()
        self.interaction.guild_id, self.interaction.channel_id = 1, 2
        self.interaction.user = SimpleNamespace(id=3, roles=[])
        self.interaction.permissions.administrator = True

    async def reset(self):
        await AiCog.new_topic.callback(AiCog(self.bot), self.interaction)

    async def test_regular_channel_resets_only_the_actor_conversation(self):
        await self.reset()
        self.bot.codex.reset_conversation.assert_awaited_once_with(conversation_key(1, 2, 3, is_thread=False))
        self.interaction.response.defer.assert_awaited_once_with(ephemeral=True)

    async def test_shared_thread_checks_current_owner_or_administrator(self):
        self.channel = MagicMock(spec=discord.Thread)
        self.channel.id, self.channel.parent_id, self.channel.owner_id = 2, 4, 5
        self.channel.permissions_for.return_value = self.permissions
        await self.reset()
        self.bot.codex.reset_conversation.assert_not_awaited()
        self.permissions.administrator = True
        self.interaction = testing.interaction()
        self.interaction.guild_id, self.interaction.channel_id = 1, 2
        self.interaction.user.id = 3
        await self.reset()
        self.bot.codex.reset_conversation.assert_awaited_once_with(conversation_key(1, 2, 3, is_thread=True))
        self.bot.ai_settings.denial_reason.assert_called_with(1, 4, frozenset())

    async def test_revocation_during_ack_and_missing_channel_block_reset(self):
        original_ack = self.interaction.response.defer.side_effect

        def ack(*args, **kwargs):
            original_ack(*args, **kwargs)
            self.permissions.view_channel = False

        self.interaction.response.defer.side_effect = ack
        await self.reset()
        self.bot.codex.reset_conversation.assert_not_awaited()
        self.channel = None
        self.interaction = testing.interaction()
        self.interaction.guild_id, self.interaction.channel_id = 1, 2
        self.interaction.user.id = 3
        await self.reset()
        self.bot.codex.reset_conversation.assert_not_awaited()

    async def test_unconfirmed_archive_is_reported_without_reconnecting_old_thread(self):
        self.bot.codex.reset_conversation.return_value.archive_unconfirmed_count = 1
        await self.reset()
        self.assertIn("封存工作會再補做", testing.text(self.interaction.followup.send.await_args.kwargs["view"]))


class ChannelDeleteTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = open_db(":memory:")
        self.addCleanup(self.db.close)
        self.bot = MagicMock(guild_id=1)
        self.bot.ai_settings = AiSettings(self.db, 1)
        self.bot.ai_settings.set_allowlist(1, frozenset({10, 11}), roles=False)
        self.bot.codex.archive_scope = AsyncMock(
            return_value=SimpleNamespace(detached_count=0, archived_count=0, archive_unconfirmed_count=0)
        )

    async def delete(self, channel_id):
        channel = MagicMock(spec=discord.TextChannel, id=channel_id, type=discord.ChannelType.text)
        channel.guild.id = 1
        await AiCog(self.bot).on_guild_channel_delete(channel)

    async def test_deleted_allowlist_channel_is_removed_even_when_ai_is_off(self):
        await self.delete(10)
        self.assertEqual(self.bot.ai_settings.channel_ids, frozenset({11}))
        self.bot.codex.archive_scope.assert_not_awaited()
        set_setting(self.db, "feature.ai", True)
        await self.delete(11)
        self.assertEqual(self.bot.ai_settings.channel_ids, frozenset())
        self.bot.codex.archive_scope.assert_awaited_once_with(1, 11, include_children=True)

    async def test_deleting_other_channel_keeps_allowlist(self):
        await self.delete(12)
        self.assertEqual(self.bot.ai_settings.channel_ids, frozenset({10, 11}))

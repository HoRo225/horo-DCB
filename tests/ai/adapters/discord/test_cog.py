import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from src.ai import AiSettings, ArchiveResult, ModelError
from src.ai.adapters.discord.cog import AiCog
from src.ai.domain.conversation import conversation_key
from src.core.adapters.discord.live import LiveGuild
from src.core.app.supervisor import REQUEST
from tests.support import testing
from tests.support.ai import FakeModel, MemoryAiStore

G, CHANNEL, USER, ROLE = 1, 2, 3, 7


class Harness:
    def __init__(self, **store: object) -> None:
        self.guild = MagicMock(id=G, unavailable=False)
        self.client = MagicMock()
        self.client.get_guild.side_effect = lambda guild_id: self.guild if guild_id == G else None
        self.client.user.id = 999
        self.model = FakeModel()
        self.store = MemoryAiStore(**({"channel_ids": (CHANNEL,), "role_ids": (ROLE,)} | store))
        self.settings = AiSettings(self.store, self.model, G)
        self.messages = SimpleNamespace(handle=AsyncMock())
        self.requests = testing.group(REQUEST)
        self.cog = AiCog(self.client, LiveGuild(self.client, G, lambda: False), self.settings, self.model,
                         self.messages, requests=self.requests)


class NewTopicTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.harness = Harness()
        self.member = SimpleNamespace(id=USER, roles=[SimpleNamespace(id=ROLE)])
        self.permissions = SimpleNamespace(view_channel=True, send_messages=True, send_messages_in_threads=True,
                                           administrator=False)
        self.channel = SimpleNamespace(id=CHANNEL, permissions_for=lambda member: self.permissions)
        guild = self.harness.guild
        guild.get_member.return_value = self.member
        guild.get_channel_or_thread.side_effect = lambda channel_id: self.channel
        self.interaction = self.new_interaction()

    def new_interaction(self):
        interaction = testing.interaction()
        interaction.guild_id, interaction.channel_id = G, CHANNEL
        interaction.user = SimpleNamespace(id=USER, roles=[])
        return interaction

    async def reset(self):
        await AiCog.new_topic.callback(self.harness.cog, self.interaction)

    def notice(self) -> str:
        return testing.text(self.interaction.followup.send.await_args.kwargs["view"])

    async def test_regular_channel_resets_only_the_actor_conversation(self):
        await self.reset()
        self.harness.model.reset_conversation.assert_awaited_once_with(conversation_key(G, CHANNEL, USER, is_thread=False))
        self.interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        self.assertIn("已開始新的對話。", self.notice())

    async def test_members_outside_the_allowlist_cannot_reset(self):
        self.member.roles = []
        await self.reset()
        self.harness.model.reset_conversation.assert_not_awaited()
        self.assertIn("你目前無法在這裡使用 AI 對話。", self.notice())

    async def test_shared_thread_checks_current_owner_or_administrator(self):
        self.harness.store.values["channels"] = [4]
        self.channel = MagicMock(spec=discord.Thread)
        self.channel.id, self.channel.parent_id, self.channel.owner_id = CHANNEL, 4, 5
        self.channel.permissions_for.return_value = self.permissions
        await self.reset()
        self.harness.model.reset_conversation.assert_not_awaited()
        self.assertIn("共享討論串只能由串主或管理員開始新話題。", self.notice())
        self.permissions.administrator = True
        self.interaction = self.new_interaction()
        await self.reset()
        self.harness.model.reset_conversation.assert_awaited_once_with(conversation_key(G, CHANNEL, USER, is_thread=True))

    async def test_revocation_during_ack_and_missing_channel_block_reset(self):
        original_ack = self.interaction.response.defer.side_effect

        def ack(*args, **kwargs):
            original_ack(*args, **kwargs)
            self.permissions.view_channel = False

        self.interaction.response.defer.side_effect = ack
        await self.reset()
        self.harness.model.reset_conversation.assert_not_awaited()
        self.channel = None
        self.interaction = self.new_interaction()
        await self.reset()
        self.harness.model.reset_conversation.assert_not_awaited()
        self.assertIn("目前無法確認對話範圍或你的權限", self.notice())

    async def test_unconfirmed_archive_is_reported_without_reconnecting_old_thread(self):
        self.harness.model.reset_conversation.return_value = ArchiveResult(1, 0, 1)
        await self.reset()
        self.assertIn("封存工作會再補做", self.notice())

    async def test_failed_reset_is_reported(self):
        self.harness.model.reset_conversation.side_effect = ModelError("unavailable")
        await self.reset()
        self.assertIn("對話重設未能確認", self.notice())


class ChannelDeleteTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.harness = Harness(enabled=False, channel_ids=(10, 11))

    async def delete(self, channel_id):
        channel = MagicMock(spec=discord.TextChannel, id=channel_id, type=discord.ChannelType.text)
        channel.guild.id = G
        await self.harness.cog.on_guild_channel_delete(channel)

    async def test_deleted_allowlist_channel_is_removed_even_when_ai_is_off(self):
        settings, model = self.harness.settings, self.harness.model
        await self.delete(10)
        self.assertEqual(settings.channel_ids, frozenset({11}))
        model.archive_scope.assert_not_awaited()
        self.harness.store.values["enabled"] = True
        await self.delete(11)
        self.assertEqual(settings.channel_ids, frozenset())
        model.archive_scope.assert_awaited_once_with(G, 11, include_children=True)

    async def test_deleting_other_channel_keeps_allowlist(self):
        await self.delete(12)
        self.assertEqual(self.harness.settings.channel_ids, frozenset({10, 11}))

    async def test_deleted_thread_archives_only_while_ai_is_on(self):
        payload = SimpleNamespace(guild_id=G, thread_id=40)
        await self.harness.cog.on_raw_thread_delete(payload)
        self.harness.model.archive_scope.assert_not_awaited()
        self.harness.store.values["enabled"] = True
        await self.harness.cog.on_raw_thread_delete(payload)
        self.harness.model.archive_scope.assert_awaited_once_with(G, 40, include_children=False)


class MemberAndMessageTest(unittest.IsolatedAsyncioTestCase):
    async def test_losing_allowlisted_role_cancels_member_requests(self) -> None:
        harness = Harness()
        allowed, other = SimpleNamespace(id=ROLE), SimpleNamespace(id=8)
        before = SimpleNamespace(roles=[allowed, other])
        after = SimpleNamespace(roles=[other], id=USER, guild=SimpleNamespace(id=G))
        await harness.cog.on_member_update(before, after)
        harness.model.cancel.assert_awaited_once_with(G, USER)
        harness.model.cancel.reset_mock()
        after.roles = [allowed]
        await harness.cog.on_member_update(before, after)
        harness.model.cancel.assert_not_called()

    async def test_message_is_a_tracked_request_until_handled(self) -> None:
        harness = Harness()
        release = asyncio.Event()

        async def handle(message, bot_user_id):
            await release.wait()

        harness.messages.handle.side_effect = handle
        message = SimpleNamespace(guild=SimpleNamespace(id=G))
        request = asyncio.create_task(harness.cog.on_message(message))
        await asyncio.sleep(0)
        self.assertEqual(harness.requests.pending(), {request})
        release.set()
        await request
        self.assertEqual(harness.requests.pending(), set())
        harness.messages.handle.assert_awaited_once_with(message, 999)

    async def test_messages_from_other_guilds_are_ignored(self) -> None:
        harness = Harness()
        await harness.cog.on_message(SimpleNamespace(guild=SimpleNamespace(id=G + 1)))
        await harness.cog.on_message(SimpleNamespace(guild=None))
        harness.messages.handle.assert_not_awaited()

    async def test_loading_starts_the_model(self) -> None:
        harness = Harness()
        await harness.cog.cog_load()
        harness.model.start.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()

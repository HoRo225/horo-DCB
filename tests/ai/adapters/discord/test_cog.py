import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from src.ai import AiSettings
from src.ai.adapters.discord.cog import AiCog
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
        model.forget_scope.assert_not_awaited()
        self.harness.store.values["enabled"] = True
        await self.delete(11)
        self.assertEqual(settings.channel_ids, frozenset())
        model.forget_scope.assert_awaited_once_with(G, 11, include_children=True)

    async def test_deleting_other_channel_keeps_allowlist(self):
        await self.delete(12)
        self.assertEqual(self.harness.settings.channel_ids, frozenset({10, 11}))

    async def test_deleted_thread_is_forgotten_only_while_ai_is_on(self):
        payload = SimpleNamespace(guild_id=G, thread_id=40)
        await self.harness.cog.on_raw_thread_delete(payload)
        self.harness.model.forget_scope.assert_not_awaited()
        self.harness.store.values["enabled"] = True
        await self.harness.cog.on_raw_thread_delete(payload)
        self.harness.model.forget_scope.assert_awaited_once_with(G, 40, include_children=False)


class MemberAndMessageTest(unittest.IsolatedAsyncioTestCase):
    async def test_losing_allowlisted_role_stops_and_forgets_the_member(self) -> None:
        harness = Harness()
        allowed, other = SimpleNamespace(id=ROLE), SimpleNamespace(id=8)
        before = SimpleNamespace(roles=[allowed, other])
        after = SimpleNamespace(roles=[other], id=USER, guild=SimpleNamespace(id=G))
        await harness.cog.on_member_update(before, after)
        harness.model.cancel.assert_awaited_once_with(G, USER)
        harness.model.forget_scope.assert_awaited_once_with(G, user_id=USER)
        harness.model.cancel.reset_mock()
        harness.model.forget_scope.reset_mock()
        after.roles = [allowed]
        await harness.cog.on_member_update(before, after)
        harness.model.cancel.assert_not_called()
        harness.model.forget_scope.assert_not_called()

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

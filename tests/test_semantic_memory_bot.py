import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from src.bot import HoroBot
from src.chat import ChatReply


class FakeTyping:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeChannel:
    def __init__(self, channel_id=10, channel_type="text", fetched=None):
        self.id = channel_id
        self.name = "general"
        self.type = channel_type
        self.fetched = fetched
        self.fetch_calls = []

    def typing(self):
        return FakeTyping()

    async def fetch_message(self, message_id):
        self.fetch_calls.append(message_id)
        if isinstance(self.fetched, Exception):
            raise self.fetched
        return self.fetched


class FakeSemanticMemory:
    def __init__(self):
        self.available = True
        self.capture_calls = []
        self.deleted_messages = []
        self.deleted_batches = []
        self.deleted_channels = []
        self.deleted_guilds = []
        self.updated = []
        self.contains = True
        self.capture_error = None

    async def capture_message(self, **kwargs):
        if self.capture_error is not None:
            raise self.capture_error
        self.capture_calls.append(kwargs)

    async def delete_message(self, message_id):
        self.deleted_messages.append(message_id)

    async def delete_messages(self, message_ids):
        self.deleted_batches.append(set(message_ids))

    async def delete_channel(self, channel_id):
        self.deleted_channels.append(channel_id)

    async def delete_guild(self, guild_id):
        self.deleted_guilds.append(guild_id)

    async def contains_message(self, _message_id):
        return self.contains

    async def update_existing_message(self, **kwargs):
        self.updated.append(kwargs)
        return True


class FakeChat:
    def __init__(self):
        self.recorded = []
        self.generated = []
        self.forgotten_channels = []
        self.lock = asyncio.Lock()

    def record_user_message(self, channel_id, name, content):
        self.recorded.append((channel_id, name, content))

    def snapshot_history(self, _channel_id):
        return tuple(self.recorded)

    def try_start_request(self, _user_id):
        return True

    def channel_lock(self, _channel_id):
        return self.lock

    def forget_channel(self, channel_id):
        self.forgotten_channels.append(channel_id)

    async def generate_reply(
        self,
        channel_id,
        context,
        *,
        image_data_urls=(),
        memory_scope=None,
        history_snapshot=None,
    ):
        self.generated.append(
            (channel_id, context, image_data_urls, memory_scope, history_snapshot)
        )
        return ChatReply("answer")


class FakeTempVoice:
    def __init__(self, error=None):
        self.error = error
        self.deleted = []

    async def handle_channel_delete(self, channel):
        self.deleted.append(channel.id)
        if self.error is not None:
            raise self.error

    async def delete_guild(self, guild_id):
        self.deleted.append(guild_id)
        if self.error is not None:
            raise self.error


def make_message(
    *,
    content="hello",
    channel_type="text",
    guild=True,
    bot_author=False,
    webhook_id=None,
    attachments=None,
    mention=False,
    message_id=1000,
):
    channel = FakeChannel(10, channel_type)
    guild_obj = SimpleNamespace(id=20, name="Guild") if guild else None
    author = SimpleNamespace(
        bot=bot_author,
        id=30,
        display_name="Alice",
    )
    return SimpleNamespace(
        id=message_id,
        guild=guild_obj,
        channel=channel,
        author=author,
        webhook_id=webhook_id,
        content=content,
        attachments=list(attachments or []),
        mentions=[SimpleNamespace(id=99)] if mention else [],
        reference=None,
        created_at=datetime(2026, 8, 22, tzinfo=timezone.utc),
        reply=AsyncMock(),
    )


class SemanticMemoryBotIngestionTest(unittest.IsolatedAsyncioTestCase):
    async def run_message(self, message, memory=None):
        semantic_memory = memory or FakeSemanticMemory()
        chat = FakeChat()
        bot = SimpleNamespace(
            user=SimpleNamespace(id=99),
            chat=chat,
            semantic_memory=semantic_memory,
            _send_ai_answer=AsyncMock(),
        )
        await HoroBot.on_message(bot, message)
        return bot, chat, semantic_memory

    async def test_unmentioned_guild_text_is_captured_without_ai_reply(self):
        message = make_message(content="我最喜歡牛肉麵")
        bot, chat, memory = await self.run_message(message)

        self.assertEqual(len(memory.capture_calls), 1)
        call = memory.capture_calls[0]
        self.assertEqual(call["message_id"], 1000)
        self.assertEqual(call["guild_id"], 20)
        self.assertEqual(call["channel_id"], 10)
        self.assertEqual(call["content"], "我最喜歡牛肉麵")
        self.assertEqual(chat.recorded, [(10, "Alice", "我最喜歡牛肉麵")])
        self.assertEqual(chat.generated, [])
        bot._send_ai_answer.assert_not_awaited()

    async def test_thread_text_is_captured(self):
        message = make_message(content="Thread memory", channel_type="public_thread")
        _bot, _chat, memory = await self.run_message(message)
        self.assertEqual(len(memory.capture_calls), 1)

    async def test_dm_bot_webhook_empty_and_pure_image_are_not_captured(self):
        cases = [
            make_message(content="dm", guild=False),
            make_message(content="bot", bot_author=True),
            make_message(content="webhook", webhook_id=123),
            make_message(content="", attachments=[]),
            make_message(content="", attachments=[SimpleNamespace()]),
        ]
        for message in cases:
            with self.subTest(content=message.content, guild=message.guild):
                _bot, _chat, memory = await self.run_message(message)
                self.assertEqual(memory.capture_calls, [])

    async def test_capture_failure_does_not_break_existing_short_term_history(self):
        memory = FakeSemanticMemory()
        memory.capture_error = RuntimeError("private-content")
        message = make_message(content="still works")

        _bot, chat, _memory = await self.run_message(message, memory)

        self.assertEqual(chat.recorded, [(10, "Alice", "still works")])

    async def test_triggered_message_receives_internal_memory_scope(self):
        message = make_message(content="<@99> 之前說過什麼？", mention=True)
        bot, chat, memory = await self.run_message(message)

        self.assertEqual(len(memory.capture_calls), 1)
        self.assertEqual(len(chat.generated), 1)
        scope = chat.generated[0][3]
        self.assertIsNotNone(scope)
        self.assertEqual(scope.channel_id, 10)
        bot._send_ai_answer.assert_awaited_once_with(message, ChatReply("answer"))


class SemanticMemoryBotEventTest(unittest.IsolatedAsyncioTestCase):
    async def test_raw_delete_bulk_thread_and_guild_cleanup(self):
        memory = FakeSemanticMemory()
        chat = FakeChat()
        bot = SimpleNamespace(semantic_memory=memory, chat=chat)

        await HoroBot.on_raw_message_delete(
            bot,
            SimpleNamespace(guild_id=20, message_id=1),
        )
        await HoroBot.on_raw_bulk_message_delete(
            bot,
            SimpleNamespace(guild_id=20, message_ids={2, 3}),
        )
        await HoroBot.on_raw_thread_delete(
            bot,
            SimpleNamespace(thread_id=40, guild_id=20),
        )
        await HoroBot.on_guild_remove(
            bot,
            SimpleNamespace(
                id=20,
                channels=[SimpleNamespace(id=50), SimpleNamespace(id=51)],
                threads=[SimpleNamespace(id=52)],
            ),
        )

        self.assertEqual(memory.deleted_messages, [1])
        self.assertEqual(memory.deleted_batches, [{2, 3}])
        self.assertEqual(memory.deleted_channels, [40])
        self.assertEqual(memory.deleted_guilds, [20])
        self.assertEqual(chat.forgotten_channels, [40, 50, 51, 52])

    async def test_channel_delete_runs_voice_and_memory_independently(self):
        memory = FakeSemanticMemory()
        temp_voice = FakeTempVoice(error=RuntimeError("voice failed"))
        bot = SimpleNamespace(semantic_memory=memory, temp_voice=temp_voice)
        channel = SimpleNamespace(id=50)

        await HoroBot.on_guild_channel_delete(bot, channel)

        self.assertEqual(temp_voice.deleted, [50])
        self.assertEqual(memory.deleted_channels, [50])

    async def test_guild_remove_cleanup_is_independent_when_temp_voice_fails(self):
        memory = FakeSemanticMemory()
        chat = FakeChat()
        temp_voice = FakeTempVoice(error=RuntimeError("voice failed"))
        calendar = SimpleNamespace(delete_guild=unittest.mock.Mock())
        bot = SimpleNamespace(
            semantic_memory=memory,
            temp_voice=temp_voice,
            chat=chat,
            calendar=calendar,
        )
        guild = SimpleNamespace(
            id=20,
            channels=[SimpleNamespace(id=50)],
            threads=[SimpleNamespace(id=51)],
        )

        await HoroBot.on_guild_remove(bot, guild)

        self.assertEqual(temp_voice.deleted, [20])
        self.assertEqual(memory.deleted_guilds, [20])
        self.assertEqual(chat.forgotten_channels, [50, 51])
        calendar.delete_guild.assert_called_once_with(20)

    async def test_guild_remove_activity_failure_does_not_block_other_cleanup(self):
        memory = FakeSemanticMemory()
        chat = FakeChat()
        temp_voice = FakeTempVoice()
        calendar = SimpleNamespace(delete_guild=unittest.mock.Mock())
        activity = SimpleNamespace(
            delete_guild=AsyncMock(side_effect=RuntimeError("activity failed"))
        )
        bot = SimpleNamespace(
            semantic_memory=memory,
            temp_voice=temp_voice,
            chat=chat,
            calendar=calendar,
            server_activity=activity,
        )
        guild = SimpleNamespace(
            id=20,
            channels=[SimpleNamespace(id=50)],
            threads=[SimpleNamespace(id=51)],
        )

        await HoroBot.on_guild_remove(bot, guild)

        activity.delete_guild.assert_awaited_once_with(20)
        calendar.delete_guild.assert_called_once_with(20)
        self.assertEqual(temp_voice.deleted, [20])
        self.assertEqual(memory.deleted_guilds, [20])
        self.assertEqual(chat.forgotten_channels, [50, 51])

    async def test_guild_remove_existing_cleanup_failures_are_independent(self):
        memory = FakeSemanticMemory()
        memory.delete_guild = AsyncMock(side_effect=RuntimeError("memory failed"))
        temp_voice = FakeTempVoice()
        chat = FakeChat()
        chat.forget_channel = unittest.mock.Mock(side_effect=RuntimeError("chat failed"))
        calendar = SimpleNamespace(
            delete_guild=unittest.mock.Mock(side_effect=RuntimeError("calendar failed"))
        )
        bot = SimpleNamespace(
            semantic_memory=memory,
            temp_voice=temp_voice,
            chat=chat,
            calendar=calendar,
        )

        await HoroBot.on_guild_remove(
            bot,
            SimpleNamespace(id=20, channels=[SimpleNamespace(id=50)], threads=[]),
        )

        self.assertEqual(temp_voice.deleted, [20])
        memory.delete_guild.assert_awaited_once_with(20)
        calendar.delete_guild.assert_called_once_with(20)

    async def test_channel_cleanup_is_safe_when_semantic_memory_is_disabled(self):
        chat = FakeChat()
        temp_voice = FakeTempVoice()
        bot = SimpleNamespace(
            semantic_memory=None,
            temp_voice=temp_voice,
            chat=chat,
        )

        await HoroBot.on_guild_channel_delete(bot, SimpleNamespace(id=51))
        await HoroBot.on_raw_thread_delete(
            bot,
            SimpleNamespace(thread_id=52, guild_id=20),
        )
        await HoroBot.on_raw_message_delete(
            bot,
            SimpleNamespace(guild_id=20, message_id=1),
        )
        await HoroBot.on_raw_bulk_message_delete(
            bot,
            SimpleNamespace(guild_id=20, message_ids={2, 3}),
        )
        await HoroBot.on_guild_remove(bot, SimpleNamespace(id=20))

        self.assertEqual(temp_voice.deleted, [51, 20])
        self.assertEqual(chat.forgotten_channels, [51, 52])

    async def test_raw_edit_updates_only_existing_human_guild_message(self):
        memory = FakeSemanticMemory()
        current = make_message(content="edited", message_id=60)
        channel = current.channel
        channel.fetched = current
        bot = SimpleNamespace(
            semantic_memory=memory,
            get_channel=lambda channel_id: channel if channel_id == 10 else None,
        )
        payload = SimpleNamespace(guild_id=20, channel_id=10, message_id=60)

        await HoroBot.on_raw_message_edit(bot, payload)

        self.assertEqual(len(memory.updated), 1)
        self.assertEqual(memory.updated[0]["content"], "edited")
        self.assertEqual(channel.fetch_calls, [60])

        memory.contains = False
        channel.fetch_calls.clear()
        await HoroBot.on_raw_message_edit(bot, payload)
        self.assertEqual(channel.fetch_calls, [])


if __name__ == "__main__":
    unittest.main()

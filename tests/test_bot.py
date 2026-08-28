import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord
from src.agent_tools import ResearchImage

from src.bot import (
    HoroBot,
    build_tool_context,
    clean_bot_mention,
    main,
    message_mentions_bot,
)
from src.chat import ChatReply
from src.discord_output import (
    AI_RESPONSE_TRUNCATION_NOTICE,
    DISCORD_MESSAGE_LIMIT,
    DISCORD_TEXT_DISPLAY_LIMIT,
    MAX_DISCORD_RESPONSE_CHUNKS,
    MAX_TEXT_DISPLAY_RESPONSE_CHUNKS,
    build_ai_text_display_view,
    split_discord_message,
    split_discord_text_display,
)


class BotHelpersTest(unittest.TestCase):
    def test_main_wires_app_config_into_services(self):
        config = SimpleNamespace(
            ninerouter_url="http://9router:20128/v1",
            ninerouter_model="test-model",
            web_search_provider="trusted-search",
            image_search_provider="trusted-images",
            web_fetch_provider="trusted-fetch",
            embedding_model="test-embedding",
            embedding_dimensions=4,
            semantic_memory_enabled=True,
            server_activity_enabled=True,
            temp_voice_enabled=False,
            steam_free_games_enabled=False,
            ai_text_display_enabled=False,
        )
        setattr(config, "discord_" + "token", "configured")
        setattr(config, "ninerouter_" + "api_" + "key", "configured")

        with (
            patch("src.bot.AppConfig.from_env", return_value=config) as from_env,
            patch("src.bot.AIClient") as ai_client_class,
            patch("src.bot.SemanticMemory") as semantic_memory_class,
            patch("src.bot.TempVoiceManager") as temp_voice_class,
            patch("src.bot.SteamFreeGamesNotifier") as steam_class,
            patch("src.bot.CalendarManager") as calendar_class,
            patch("src.bot.ServerActivityMonitor") as activity_class,
            patch("src.bot.AgentTools") as agent_tools_class,
            patch("src.bot.ChatManager") as chat_class,
            patch("src.bot.HoroBot") as bot_class,
        ):
            main()

        from_env.assert_called_once_with()
        ai_client_class.assert_called_once_with(
            config.ninerouter_url,
            getattr(config, "ninerouter_" + "api_" + "key"),
            config.ninerouter_model,
        )
        semantic_memory_class.assert_called_once_with(
            ai_client_class.return_value,
            embedding_model=config.embedding_model,
            embedding_dimensions=config.embedding_dimensions,
        )
        calendar_class.assert_called_once_with()
        activity_class.assert_called_once_with()
        agent_tools_class.assert_called_once_with(
            steam_class.return_value,
            ai_client_class.return_value,
            semantic_memory=semantic_memory_class.return_value,
            calendar=calendar_class.return_value,
            search_provider=config.web_search_provider,
            image_search_provider=config.image_search_provider,
            fetch_provider=config.web_fetch_provider,
        )
        chat_class.assert_called_once_with(
            ai_client_class.return_value,
            agent_tools_class.return_value,
        )
        bot_class.assert_called_once_with(
            chat_class.return_value,
            temp_voice_class.return_value,
            steam_class.return_value,
            semantic_memory=semantic_memory_class.return_value,
            calendar=calendar_class.return_value,
            server_activity=activity_class.return_value,
            ai_client=ai_client_class.return_value,
            ai_text_display_enabled=False,
            temp_voice_enabled=False,
            steam_free_games_enabled=False,
        )
        bot_class.return_value.run.assert_called_once_with(
            getattr(config, "discord_" + "token"),
            log_handler=None,
        )

    def test_build_tool_context_for_guild_text_channel(self):
        message = SimpleNamespace(
            guild=SimpleNamespace(name="Horo Guild", id=123),
            channel=SimpleNamespace(name="general", type="text", id=456),
        )

        context = build_tool_context(message)

        self.assertEqual(context.guild_name, "Horo Guild")
        self.assertEqual(context.channel_name, "general")
        self.assertEqual(context.channel_type, "text")
        self.assertFalse(hasattr(context, "guild_id"))
        self.assertFalse(hasattr(context, "channel_id"))
        self.assertFalse(hasattr(context, "user_id"))

    def test_build_tool_context_for_direct_message(self):
        message = SimpleNamespace(
            guild=None,
            channel=SimpleNamespace(),
        )

        context = build_tool_context(message)

        self.assertIsNone(context.guild_name)
        self.assertEqual(context.channel_name, "direct-message")
        self.assertEqual(context.channel_type, "unknown")

    def test_plain_message_does_not_mention_bot(self):
        message = SimpleNamespace(mentions=[])
        self.assertFalse(message_mentions_bot(message, 123))

    def test_mention_triggers_bot(self):
        message = SimpleNamespace(mentions=[SimpleNamespace(id=123)])
        self.assertTrue(message_mentions_bot(message, 123))

    def test_clean_bot_mention(self):
        self.assertEqual(clean_bot_mention("<@123> 你好", 123), "你好")
        self.assertEqual(clean_bot_mention("<@!123> 你好", 123), "你好")

    def test_short_native_reply_is_unchanged(self):
        self.assertEqual(split_discord_message(""), [])
        text = "x" * DISCORD_MESSAGE_LIMIT
        self.assertEqual(split_discord_message(text), [text])

    def test_long_native_reply_is_split_at_discord_limit(self):
        text = "x" * 4_500
        chunks = split_discord_message(text)

        self.assertEqual("".join(chunks), text)
        self.assertEqual([len(chunk) for chunk in chunks], [2_000, 2_000, 500])
        self.assertTrue(all(len(chunk) <= DISCORD_MESSAGE_LIMIT for chunk in chunks))

    def test_long_native_reply_prefers_natural_boundaries(self):
        cases = (
            ("a" * 1_200 + "\n\n" + "b" * 1_000, "\n\n"),
            ("a" * 1_200 + "\n" + "b" * 1_000, "\n"),
            ("a" * 1_200 + " " + "b" * 1_000, " "),
        )
        for text, separator in cases:
            with self.subTest(separator=repr(separator)):
                chunks = split_discord_message(text)
                self.assertEqual("".join(chunks), text)
                self.assertEqual(len(chunks), 2)
                self.assertTrue(chunks[0].endswith(separator))
                self.assertTrue(
                    all(len(chunk) <= DISCORD_MESSAGE_LIMIT for chunk in chunks)
                )

    def test_long_code_block_reopens_fence_across_messages(self):
        text = "```python\n" + "x" * 2_500 + "\n```"
        chunks = split_discord_message(text)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(chunks[0].startswith("```python\n"))
        self.assertTrue(chunks[0].endswith("\n```"))
        self.assertTrue(chunks[1].startswith("```python\n"))
        self.assertTrue(all(chunk.count("```") % 2 == 0 for chunk in chunks))
        self.assertTrue(all(len(chunk) <= DISCORD_MESSAGE_LIMIT for chunk in chunks))

    def test_oversize_code_block_closes_before_truncation_notice(self):
        text = "```python\n" + "x" * (
            DISCORD_MESSAGE_LIMIT * MAX_DISCORD_RESPONSE_CHUNKS + 1_000
        )
        chunks = split_discord_message(text)
        last = chunks[-1]

        self.assertEqual(len(chunks), MAX_DISCORD_RESPONSE_CHUNKS)
        self.assertTrue(last.endswith(AI_RESPONSE_TRUNCATION_NOTICE))
        self.assertTrue(last.count("```") % 2 == 0)
        self.assertLess(last.rfind("```"), last.rfind(AI_RESPONSE_TRUNCATION_NOTICE))
        self.assertTrue(all(len(chunk) <= DISCORD_MESSAGE_LIMIT for chunk in chunks))

    def test_oversize_native_reply_is_capped_with_notice(self):
        text = "x" * (
            DISCORD_MESSAGE_LIMIT * MAX_DISCORD_RESPONSE_CHUNKS + 1_000
        )
        chunks = split_discord_message(text)
        self.assertEqual(len(chunks), MAX_DISCORD_RESPONSE_CHUNKS)
        self.assertTrue(chunks[-1].endswith(AI_RESPONSE_TRUNCATION_NOTICE))
        self.assertTrue(all(len(chunk) <= DISCORD_MESSAGE_LIMIT for chunk in chunks))

    def test_small_valid_limit_still_respects_requested_limit(self):
        chunks = split_discord_message("x" * 100, 10)
        self.assertTrue(all(len(chunk) <= 10 for chunk in chunks))

    def test_invalid_native_reply_limit_is_rejected(self):
        with self.assertRaises(ValueError):
            split_discord_message("hello", 0)
        with self.assertRaises(ValueError):
            split_discord_message("hello", DISCORD_MESSAGE_LIMIT + 1)

    def test_text_display_split_uses_4000_and_preserves_response_cap(self):
        text = "x" * 9_000
        chunks = split_discord_text_display(text)

        self.assertEqual("".join(chunks), text)
        self.assertEqual([len(chunk) for chunk in chunks], [4_000, 4_000, 1_000])
        self.assertTrue(all(len(chunk) <= DISCORD_TEXT_DISPLAY_LIMIT for chunk in chunks))

        oversize = "x" * (
            DISCORD_TEXT_DISPLAY_LIMIT * MAX_TEXT_DISPLAY_RESPONSE_CHUNKS + 1_000
        )
        capped = split_discord_text_display(oversize)
        self.assertEqual(len(capped), MAX_TEXT_DISPLAY_RESPONSE_CHUNKS)
        self.assertTrue(capped[-1].endswith(AI_RESPONSE_TRUNCATION_NOTICE))
        self.assertTrue(all(len(chunk) <= DISCORD_TEXT_DISPLAY_LIMIT for chunk in capped))

    def test_text_display_view_is_one_top_level_text_display(self):
        content = "# 標題\n\n`code`"
        view = build_ai_text_display_view(content)

        self.assertIsInstance(view, discord.ui.LayoutView)
        self.assertEqual(view.content_length(), len(content))
        self.assertEqual(len(view.children), 1)
        self.assertIsInstance(view.children[0], discord.ui.TextDisplay)
        self.assertEqual(view.children[0].content, content)

    def test_text_display_view_adds_media_gallery_after_text(self):
        view = build_ai_text_display_view(
            "answer", (ResearchImage("https://images.example.com/one.jpg", "One"),)
        )

        self.assertEqual(len(view.children), 2)
        self.assertIsInstance(view.children[0], discord.ui.TextDisplay)
        self.assertIsInstance(view.children[1], discord.ui.MediaGallery)


class BotSendTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _http_error(message="send failed"):
        return discord.HTTPException(
            SimpleNamespace(status=500, reason="test"),
            message,
        )

    async def test_multichunk_reply_puts_gallery_only_in_first_text_display(self):
        class FakeChat:
            def __init__(self):
                self.recorded = []

            def record_assistant_message(self, channel_id, content):
                self.recorded.append((channel_id, content))

        class FakeChannel:
            id = 123

            def __init__(self):
                self.sends = []

            async def send(self, content=None, **kwargs):
                self.sends.append((content, kwargs))

        class FakeMessage:
            def __init__(self):
                self.channel = FakeChannel()
                self.replies = []

            async def reply(self, content=None, **kwargs):
                self.replies.append((content, kwargs))

        chat = FakeChat()
        bot = SimpleNamespace(chat=chat)
        message = FakeMessage()
        answer = "a" * 5_000

        with patch("src.bot.AI_TEXT_DISPLAY_ENABLED", True):
            await HoroBot._send_ai_answer(
                bot,
                message,
                ChatReply(answer, (ResearchImage("https://images.example.com/one.jpg", "One"),)),
            )

        self.assertEqual(len(message.replies), 1)
        self.assertIsNone(message.replies[0][0])
        first_view = message.replies[0][1]["view"]
        self.assertIsInstance(first_view, discord.ui.LayoutView)
        self.assertEqual(first_view.children[0].content, "a" * 4_000)
        self.assertFalse(message.replies[0][1]["mention_author"])
        self.assertEqual(
            message.replies[0][1]["allowed_mentions"].to_dict(),
            discord.AllowedMentions.none().to_dict(),
        )

        self.assertEqual(len(message.channel.sends), 1)
        self.assertIsNone(message.channel.sends[0][0])
        second_view = message.channel.sends[0][1]["view"]
        self.assertEqual(second_view.children[0].content, "a" * 1_000)
        self.assertEqual(len(first_view.children), 2)
        self.assertIsInstance(first_view.children[1], discord.ui.MediaGallery)
        self.assertEqual(len(second_view.children), 1)
        self.assertEqual(
            message.channel.sends[0][1]["allowed_mentions"].to_dict(),
            discord.AllowedMentions.none().to_dict(),
        )
        self.assertEqual(chat.recorded, [(message.channel.id, answer)])

    async def test_first_text_display_failure_falls_back_to_full_native_reply(self):
        class FakeChat:
            def __init__(self):
                self.recorded = []

            def record_assistant_message(self, channel_id, content):
                self.recorded.append((channel_id, content))

        class FakeChannel:
            id = 123

            def __init__(self):
                self.sends = []

            async def send(self, content=None, **kwargs):
                self.sends.append((content, kwargs))

        class FakeMessage:
            def __init__(self):
                self.channel = FakeChannel()
                self.replies = []

            async def reply(self, content=None, **kwargs):
                self.replies.append((content, kwargs))
                if "view" in kwargs:
                    raise BotSendTest._http_error()

        chat = FakeChat()
        bot = SimpleNamespace(chat=chat)
        message = FakeMessage()
        answer = "a" * 2_500

        with (
            patch("src.bot.AI_TEXT_DISPLAY_ENABLED", True),
            patch("src.bot.logging.exception") as log_exception,
        ):
            await HoroBot._send_ai_answer(
                bot,
                message,
                ChatReply(answer, (ResearchImage("https://images.example.com/one.jpg", "One"),)),
            )

        self.assertEqual(len(message.replies), 2)
        self.assertIsNone(message.replies[0][0])
        self.assertIn("view", message.replies[0][1])
        self.assertEqual(message.replies[1][0], "a" * 2_000)
        self.assertNotIn("view", message.replies[1][1])
        self.assertEqual(message.channel.sends[0][0], "a" * 500)
        self.assertEqual(chat.recorded, [(message.channel.id, answer)])
        log_exception.assert_called_once_with(
            "Discord AI TextDisplay 回覆送出失敗，改用原生文字。"
        )

    async def test_partial_text_display_failure_falls_back_only_remaining_content(self):
        class FakeChat:
            def __init__(self):
                self.recorded = []

            def record_assistant_message(self, channel_id, content):
                self.recorded.append((channel_id, content))

        class FakeChannel:
            id = 123

            def __init__(self):
                self.sends = []
                self.failed_view = False

            async def send(self, content=None, **kwargs):
                self.sends.append((content, kwargs))
                if "view" in kwargs and not self.failed_view:
                    self.failed_view = True
                    raise BotSendTest._http_error()

        class FakeMessage:
            def __init__(self):
                self.channel = FakeChannel()
                self.replies = []

            async def reply(self, content=None, **kwargs):
                self.replies.append((content, kwargs))

        chat = FakeChat()
        bot = SimpleNamespace(chat=chat)
        message = FakeMessage()
        answer = "a" * 5_000

        with (
            patch("src.bot.AI_TEXT_DISPLAY_ENABLED", True),
            patch("src.bot.logging.exception") as log_exception,
        ):
            await HoroBot._send_ai_answer(bot, message, ChatReply(answer))

        self.assertEqual(len(message.replies), 1)
        self.assertIsNone(message.replies[0][0])
        self.assertEqual(
            message.replies[0][1]["view"].children[0].content,
            "a" * 4_000,
        )
        self.assertEqual(len(message.channel.sends), 2)
        self.assertIn("view", message.channel.sends[0][1])
        self.assertEqual(message.channel.sends[1][0], "a" * 1_000)
        self.assertNotIn("view", message.channel.sends[1][1])
        self.assertEqual(chat.recorded, [(message.channel.id, answer)])
        log_exception.assert_called_once_with(
            "Discord AI TextDisplay 回覆送出失敗，改用原生文字。"
        )

    async def test_native_fallback_failure_records_only_successfully_sent_content(self):
        class FakeChat:
            def __init__(self):
                self.recorded = []

            def record_assistant_message(self, channel_id, content):
                self.recorded.append((channel_id, content))

        class FakeChannel:
            id = 123

            async def send(self, content=None, **kwargs):
                if content is not None:
                    raise BotSendTest._http_error()

        class FakeMessage:
            def __init__(self):
                self.channel = FakeChannel()
                self.replies = []

            async def reply(self, content=None, **kwargs):
                self.replies.append((content, kwargs))
                if "view" in kwargs:
                    raise BotSendTest._http_error()

        chat = FakeChat()
        bot = SimpleNamespace(chat=chat)
        message = FakeMessage()
        answer = "a" * 4_500

        with (
            patch("src.bot.AI_TEXT_DISPLAY_ENABLED", True),
            patch("src.bot.logging.exception") as log_exception,
        ):
            await HoroBot._send_ai_answer(bot, message, ChatReply(answer))

        self.assertEqual(message.replies[1][0], "a" * DISCORD_MESSAGE_LIMIT)
        self.assertEqual(
            chat.recorded,
            [(message.channel.id, "a" * DISCORD_MESSAGE_LIMIT)],
        )
        self.assertEqual(log_exception.call_count, 2)
        log_exception.assert_any_call(
            "Discord AI TextDisplay 回覆送出失敗，改用原生文字。"
        )
        log_exception.assert_any_call("Discord AI 回覆送出失敗。")

    async def test_feature_switch_with_images_sends_native_text_without_gallery(self):
        class FakeChat:
            def __init__(self):
                self.recorded = []

            def record_assistant_message(self, channel_id, content):
                self.recorded.append((channel_id, content))

        class FakeChannel:
            id = 123

            def __init__(self):
                self.sends = []

            async def send(self, content=None, **kwargs):
                self.sends.append((content, kwargs))

        class FakeMessage:
            def __init__(self):
                self.channel = FakeChannel()
                self.replies = []

            async def reply(self, content=None, **kwargs):
                self.replies.append((content, kwargs))

        chat = FakeChat()
        bot = SimpleNamespace(chat=chat)
        message = FakeMessage()
        answer = "a" * 2_500

        with patch("src.bot.AI_TEXT_DISPLAY_ENABLED", False):
            await HoroBot._send_ai_answer(
                bot,
                message,
                ChatReply(answer, (ResearchImage("https://images.example.com/one.jpg", "One"),)),
            )

        self.assertEqual(message.replies[0][0], "a" * 2_000)
        self.assertNotIn("view", message.replies[0][1])
        self.assertEqual(message.channel.sends[0][0], "a" * 500)
        self.assertNotIn("view", message.channel.sends[0][1])
        self.assertEqual(chat.recorded, [(message.channel.id, answer)])


class BotLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_guild_join_reconciles_only_joined_guild_when_enabled(self):
        guild = SimpleNamespace(id=20)
        temp_voice = SimpleNamespace(reconcile=AsyncMock())
        bot = SimpleNamespace(temp_voice=temp_voice, temp_voice_enabled=True)

        await HoroBot.on_guild_join(bot, guild)

        temp_voice.reconcile.assert_awaited_once_with([guild], prune_absent=False)

    async def test_guild_join_does_not_reconcile_when_disabled(self):
        guild = SimpleNamespace(id=20)
        temp_voice = SimpleNamespace(reconcile=AsyncMock())
        bot = SimpleNamespace(temp_voice=temp_voice, temp_voice_enabled=False)

        await HoroBot.on_guild_join(bot, guild)

        temp_voice.reconcile.assert_not_awaited()

    async def test_guild_join_enables_activity_when_temp_voice_disabled(self):
        guild = SimpleNamespace(id=20)
        activity = SimpleNamespace(enable_guild=AsyncMock())
        bot = SimpleNamespace(
            server_activity=activity,
            temp_voice=SimpleNamespace(reconcile=AsyncMock()),
            temp_voice_enabled=False,
        )

        await HoroBot.on_guild_join(bot, guild)

        activity.enable_guild.assert_awaited_once_with(20)

    async def test_server_activity_controls_member_intent_without_presence_intent(self):
        enabled = HoroBot(
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            server_activity=object(),
            ai_client=SimpleNamespace(),
        )
        disabled = HoroBot(
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            ai_client=SimpleNamespace(),
        )

        self.assertTrue(enabled.intents.members)
        self.assertFalse(enabled.intents.presences)
        self.assertFalse(disabled.intents.members)

    async def test_setup_hook_starts_ai_client_before_dependent_services(self):
        calls = []
        ai_client = SimpleNamespace(
            start=AsyncMock(side_effect=lambda: calls.append("ai_client.start")),
        )
        semantic_memory = SimpleNamespace(
            start=AsyncMock(side_effect=lambda: calls.append("semantic_memory.start")),
        )
        server_activity = SimpleNamespace(
            start=AsyncMock(side_effect=lambda: calls.append("server_activity.start")),
        )
        steam = SimpleNamespace(start=Mock(), close=AsyncMock())
        bot = HoroBot(
            SimpleNamespace(),
            SimpleNamespace(),
            steam,
            semantic_memory=semantic_memory,
            server_activity=server_activity,
            ai_client=ai_client,
        )

        with patch.object(bot.tree, "sync", new=AsyncMock()) as sync:
            await bot.setup_hook()

        ai_client.start.assert_awaited_once_with()
        semantic_memory.start.assert_awaited_once_with()
        server_activity.start.assert_awaited_once_with()
        self.assertLess(
            calls.index("ai_client.start"),
            calls.index("semantic_memory.start"),
        )
        sync.assert_awaited_once_with()

    async def test_close_orders_semantic_memory_ai_client_and_discord_client(self):
        calls = []
        ai_client = SimpleNamespace(
            close=AsyncMock(side_effect=lambda: calls.append("ai_client.close")),
        )
        semantic_memory = SimpleNamespace(
            close=AsyncMock(side_effect=lambda: calls.append("semantic_memory.close")),
        )
        server_activity = SimpleNamespace(
            close=AsyncMock(side_effect=lambda: calls.append("server_activity.close")),
        )
        steam = SimpleNamespace(
            close=AsyncMock(side_effect=lambda: calls.append("steam.close")),
        )
        bot = HoroBot(
            SimpleNamespace(),
            SimpleNamespace(),
            steam,
            semantic_memory=semantic_memory,
            server_activity=server_activity,
            ai_client=ai_client,
        )

        with patch.object(
            discord.Client,
            "close",
            new=AsyncMock(side_effect=lambda: calls.append("discord.close")),
        ) as discord_close:
            await bot.close()

        semantic_memory.close.assert_awaited_once_with()
        ai_client.close.assert_awaited_once_with()
        server_activity.close.assert_awaited_once_with()
        discord_close.assert_awaited_once_with()
        self.assertLess(
            calls.index("semantic_memory.close"),
            calls.index("ai_client.close"),
        )
        self.assertLess(
            calls.index("ai_client.close"),
            calls.index("server_activity.close"),
        )
        self.assertLess(
            calls.index("server_activity.close"),
            calls.index("discord.close"),
        )

    async def test_close_continues_in_order_after_each_service_failure(self):
        service_names = (
            "steam",
            "calendar",
            "semantic_memory",
            "ai_client",
            "server_activity",
        )

        for failing_service in service_names:
            with self.subTest(failing_service=failing_service):
                calls = []

                def close_for(name):
                    async def close():
                        calls.append(f"{name}.close")
                        if name == failing_service:
                            raise RuntimeError("secret-value-must-not-be-logged")

                    return AsyncMock(side_effect=close)

                services = {
                    name: SimpleNamespace(close=close_for(name))
                    for name in service_names
                }
                bot = HoroBot(
                    SimpleNamespace(),
                    SimpleNamespace(),
                    services["steam"],
                    semantic_memory=services["semantic_memory"],
                    calendar=services["calendar"],
                    server_activity=services["server_activity"],
                    ai_client=services["ai_client"],
                )

                with (
                    patch.object(
                        discord.Client,
                        "close",
                        new=AsyncMock(
                            side_effect=lambda: calls.append("discord.close")
                        ),
                    ),
                    self.assertLogs(level="ERROR") as captured,
                ):
                    await bot.close()

                self.assertEqual(
                    calls,
                    [
                        "steam.close",
                        "calendar.close",
                        "semantic_memory.close",
                        "ai_client.close",
                        "server_activity.close",
                        "discord.close",
                    ],
                )
                self.assertEqual(
                    captured.output,
                    ["ERROR:root:Bot service shutdown failed."],
                )
                self.assertNotIn("secret-value-must-not-be-logged", captured.output[0])

    async def test_close_propagates_cancellation_after_attempting_discord_close(self):
        calls = []

        async def cancel_calendar():
            calls.append("calendar.close")
            raise asyncio.CancelledError

        bot = HoroBot(
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(
                close=AsyncMock(side_effect=lambda: calls.append("steam.close"))
            ),
            semantic_memory=SimpleNamespace(
                close=AsyncMock(
                    side_effect=lambda: calls.append("semantic_memory.close")
                )
            ),
            calendar=SimpleNamespace(close=AsyncMock(side_effect=cancel_calendar)),
            server_activity=SimpleNamespace(
                close=AsyncMock(
                    side_effect=lambda: calls.append("server_activity.close")
                )
            ),
            ai_client=SimpleNamespace(
                close=AsyncMock(side_effect=lambda: calls.append("ai_client.close"))
            ),
        )

        with patch.object(
            discord.Client,
            "close",
            new=AsyncMock(side_effect=lambda: calls.append("discord.close")),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await bot.close()

        self.assertEqual(
            calls,
            ["steam.close", "calendar.close", "discord.close"],
        )


class ServerActivityRoutingTest(unittest.IsolatedAsyncioTestCase):
    async def test_all_activity_callback_mappings(self):
        monitor = Mock()
        bot = HoroBot(
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            server_activity=monitor,
            ai_client=SimpleNamespace(),
            temp_voice_enabled=False,
        )
        before = object()
        after = object()
        event = object()
        user = object()
        payload = object()

        await bot.on_audit_log_entry_create(event)
        await bot.on_member_join(event)
        await bot.on_raw_member_remove(payload)
        await bot.on_member_update(before, after)
        await bot.on_raw_reaction_add(payload)
        await bot.on_raw_reaction_remove(payload)
        await bot.on_raw_reaction_clear(payload)
        await bot.on_raw_reaction_clear_emoji(payload)
        await bot.on_raw_poll_vote_add(payload)
        await bot.on_raw_poll_vote_remove(payload)
        await bot.on_thread_create(event)
        await bot.on_thread_update(before, after)
        await bot.on_scheduled_event_user_add(event, user)
        await bot.on_scheduled_event_user_remove(event, user)
        await bot.on_automod_action(event)

        monitor.record_audit.assert_called_once_with(event)
        self.assertEqual(
            monitor.record_member.call_args_list,
            [
                unittest.mock.call("member_join", event),
                unittest.mock.call("member_update", before, after),
            ],
        )
        monitor.record_raw_member_remove.assert_called_once_with(payload)
        self.assertEqual(
            monitor.record_reaction.call_args_list,
            [
                unittest.mock.call("reaction_add", payload),
                unittest.mock.call("reaction_remove", payload),
                unittest.mock.call("reaction_clear", payload),
                unittest.mock.call("reaction_clear_emoji", payload),
            ],
        )
        self.assertEqual(
            monitor.record_poll_vote.call_args_list,
            [
                unittest.mock.call("poll_vote_add", payload),
                unittest.mock.call("poll_vote_remove", payload),
            ],
        )
        self.assertEqual(
            monitor.record_thread.call_args_list,
            [
                unittest.mock.call("thread_create", event),
                unittest.mock.call("thread_update", after),
            ],
        )
        self.assertEqual(
            monitor.record_scheduled_subscriber.call_args_list,
            [
                unittest.mock.call("scheduled_event_user_add", event, user),
                unittest.mock.call("scheduled_event_user_remove", event, user),
            ],
        )
        monitor.record_automod.assert_called_once_with(event)

    def test_horo_bot_does_not_define_cached_member_remove_handler(self):
        self.assertNotIn("on_member_remove", HoroBot.__dict__)

    async def test_activity_failure_does_not_block_voice_or_semantic_cleanup(self):
        monitor = SimpleNamespace(
            record_voice=Mock(side_effect=RuntimeError("activity failed")),
            record_message=Mock(side_effect=RuntimeError("activity failed")),
        )
        temp_voice = SimpleNamespace(handle_voice_state_update=AsyncMock())
        semantic_memory = SimpleNamespace(delete_message=AsyncMock())
        bot = HoroBot(
            SimpleNamespace(),
            temp_voice,
            SimpleNamespace(),
            semantic_memory=semantic_memory,
            server_activity=monitor,
            ai_client=SimpleNamespace(),
        )
        member = object()
        before = object()
        after = object()
        payload = SimpleNamespace(
            guild_id=1,
            channel_id=2,
            message_id=3,
        )

        await bot.on_voice_state_update(member, before, after)
        await bot.on_raw_message_delete(payload)

        temp_voice.handle_voice_state_update.assert_awaited_once_with(member, before, after)
        semantic_memory.delete_message.assert_awaited_once_with(3)


if __name__ == "__main__":
    unittest.main()

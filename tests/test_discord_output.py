from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

from src.bot import HoroBot
from src.discord_output import (
    AI_RESPONSE_TRUNCATION_NOTICE,
    DISCORD_MESSAGE_LIMIT,
    MAX_DISCORD_RESPONSE_CHUNKS,
    build_ai_text_display_view,
    split_discord_message,
    split_discord_text_display,
)


class DiscordOutputTest(unittest.TestCase):
    def test_short_and_long_markdown_respect_discord_limits(self):
        self.assertEqual(split_discord_message("hello"), ["hello"])

        chunks = split_discord_message("a" * 2500)

        self.assertEqual([len(chunk) for chunk in chunks], [2000, 500])
        self.assertTrue(all(len(chunk) <= DISCORD_MESSAGE_LIMIT for chunk in chunks))

    def test_oversize_reply_is_capped_with_notice(self):
        chunks = split_discord_message(
            "x" * (DISCORD_MESSAGE_LIMIT * MAX_DISCORD_RESPONSE_CHUNKS + 1000)
        )

        self.assertEqual(len(chunks), MAX_DISCORD_RESPONSE_CHUNKS)
        self.assertTrue(chunks[-1].endswith(AI_RESPONSE_TRUNCATION_NOTICE))

    def test_code_fence_is_balanced_across_chunks(self):
        fence = chr(96) * 3
        chunks = split_discord_message(fence + "python\n" + "x" * 3000 + "\n" + fence)

        self.assertTrue(chunks[0].endswith("\n" + fence))
        self.assertTrue(chunks[1].startswith(fence + "python\n"))
        self.assertTrue(all(chunk.count(fence) % 2 == 0 for chunk in chunks))

    def test_text_display_uses_4000_character_chunks(self):
        chunks = split_discord_text_display("x" * 9000)

        self.assertEqual([len(chunk) for chunk in chunks], [4000, 4000, 1000])

    def test_text_display_view_contains_only_requested_text(self):
        view = build_ai_text_display_view("# heading")

        self.assertIsInstance(view, discord.ui.LayoutView)
        self.assertEqual(len(view.children), 1)
        self.assertEqual(view.children[0].content, "# heading")


class BotOutputTest(unittest.IsolatedAsyncioTestCase):
    async def test_bot_sends_codex_answer_without_mentions(self):
        class Channel:
            def __init__(self):
                self.sent = []

            async def send(self, content=None, **kwargs):
                self.sent.append((content, kwargs))

        class Message:
            def __init__(self):
                self.channel = Channel()
                self.replies = []

            async def reply(self, content=None, **kwargs):
                self.replies.append((content, kwargs))

        message = Message()
        bot = SimpleNamespace(ai_text_display_enabled=False)

        await HoroBot._send_ai_answer(bot, message, "a" * 2500)

        self.assertEqual(message.replies[0][0], "a" * 2000)
        self.assertEqual(message.channel.sent[0][0], "a" * 500)
        self.assertFalse(message.replies[0][1]["allowed_mentions"].everyone)
        self.assertFalse(message.replies[0][1]["mention_author"])

    async def test_text_display_http_failure_falls_back_to_native_text(self):
        response = SimpleNamespace(status=400, reason="Bad Request")
        failure = discord.HTTPException(response, "bad")
        message = SimpleNamespace(
            reply=AsyncMock(side_effect=[failure, None]),
            channel=SimpleNamespace(send=AsyncMock()),
        )
        bot = SimpleNamespace(ai_text_display_enabled=True)

        with patch(
            "src.bot.build_ai_text_display_view",
            return_value=discord.ui.LayoutView(),
        ):
            await HoroBot._send_ai_answer(bot, message, "answer")

        self.assertEqual(message.reply.await_count, 2)
        fallback = message.reply.await_args_list[1]
        self.assertEqual(fallback.args[0], "answer")
        self.assertFalse(fallback.kwargs["allowed_mentions"].everyone)
        self.assertFalse(fallback.kwargs["mention_author"])


if __name__ == "__main__":
    unittest.main()

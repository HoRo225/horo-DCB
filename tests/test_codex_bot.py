import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

from src.bot import (
    HoroBot,
    codex_conversation_key_for_message,
    codex_error_text,
)
from src.codex_bridge_client import CodexAccess, CodexBridgeError


class Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb):
        return False


class FakeCodex:
    def __init__(self, reply="answer", *, allowed=True, error=None):
        self.reply = reply
        self.allowed = allowed
        self.error = error
        self.calls = []

    def try_start_request(self, _user_id):
        return self.allowed

    def conversation_lock(self, _key):
        return asyncio.Lock()

    async def chat(self, key, display_name, text, images):
        if self.error is not None:
            raise self.error
        self.calls.append((key, display_name, text, images))
        return self.reply


def make_message(
    *,
    user_id=30,
    role_ids=(),
    administrator=False,
    channel_type=discord.ChannelType.text,
    parent_id=None,
):
    channel = SimpleNamespace(
        id=20,
        parent_id=parent_id,
        type=channel_type,
        typing=lambda: Typing(),
    )
    message = SimpleNamespace(
        author=SimpleNamespace(
            id=user_id,
            bot=False,
            display_name="Steven",
            roles=[SimpleNamespace(id=role_id) for role_id in role_ids],
            guild_permissions=SimpleNamespace(administrator=administrator),
        ),
        webhook_id=None,
        guild=SimpleNamespace(id=10),
        channel=channel,
        content="<@99> hello",
        attachments=[],
        mentions=[SimpleNamespace(id=99)],
        reference=None,
        reply=AsyncMock(),
    )
    return message


class CodexBotHelpersTest(unittest.TestCase):
    def test_allowlisted_channel_and_thread_build_expected_keys(self):
        access = CodexAccess(True, 10, 20, frozenset({30}))
        normal = make_message()
        thread = make_message(
            channel_type=discord.ChannelType.public_thread,
            parent_id=20,
        )
        thread.channel.id = 21

        self.assertEqual(
            codex_conversation_key_for_message(normal, access),
            "guild:10:channel:20:user:30",
        )
        self.assertEqual(
            codex_conversation_key_for_message(thread, access),
            "guild:10:thread:21",
        )

    def test_scope_rejects_dm_wrong_parent_and_wrong_user(self):
        access = CodexAccess(True, 10, 20, frozenset({30}))
        wrong_channel = make_message()
        wrong_channel.channel.id = 22
        wrong_user = make_message(user_id=31)
        direct_message = make_message()
        direct_message.guild = None

        self.assertIsNone(codex_conversation_key_for_message(wrong_channel, access))
        self.assertIsNone(codex_conversation_key_for_message(wrong_user, access))
        self.assertIsNone(codex_conversation_key_for_message(direct_message, access))

    def test_roles_replace_legacy_user_without_admin_bypass(self):
        access = CodexAccess(True, 10, 20, frozenset({30}))
        access.set_roles(10, frozenset({70, 80}))
        allowed = make_message(user_id=31, role_ids=(60, 80))
        legacy_only = make_message(user_id=30)
        administrator = make_message(user_id=32, administrator=True)

        self.assertEqual(
            codex_conversation_key_for_message(allowed, access),
            "guild:10:channel:20:user:31",
        )
        self.assertIsNone(codex_conversation_key_for_message(legacy_only, access))
        self.assertIsNone(codex_conversation_key_for_message(administrator, access))

    def test_role_mode_fails_closed_when_member_roles_are_missing(self):
        access = CodexAccess(True, 10, 20, frozenset({30}))
        access.set_roles(10, frozenset({70}))
        message = make_message(user_id=31, role_ids=(70,))
        del message.author.roles

        self.assertIsNone(codex_conversation_key_for_message(message, access))

    def test_errors_map_to_fixed_user_safe_text(self):
        self.assertIn("登入", codex_error_text("auth_required"))
        self.assertIn("逾時", codex_error_text("timeout"))
        self.assertIn("額度", codex_error_text("usage_limit_or_unavailable"))
        self.assertEqual(codex_error_text("internal details"), codex_error_text("unavailable"))


class CodexBotRoutingTest(unittest.IsolatedAsyncioTestCase):
    async def test_allowlisted_mention_calls_codex_and_sends_answer(self):
        message = make_message()
        codex = FakeCodex()
        bot = SimpleNamespace(
            user=SimpleNamespace(id=99),
            codex_access=CodexAccess(True, 10, 20, frozenset({30})),
            codex=codex,
            server_activity=None,
            _send_ai_answer=AsyncMock(),
        )

        with patch("src.bot.read_image_attachments", AsyncMock(return_value=())):
            await HoroBot.on_message(bot, message)

        self.assertEqual(
            codex.calls,
            [("guild:10:channel:20:user:30", "Steven", "hello", ())],
        )
        bot._send_ai_answer.assert_awaited_once_with(message, "answer")

    async def test_allowlisted_reply_to_bot_calls_codex(self):
        message = make_message()
        message.content = "follow up"
        message.mentions = []
        message.reference = SimpleNamespace(
            channel_id=20,
            message_id=55,
            resolved=SimpleNamespace(
                author=SimpleNamespace(id=99),
                attachments=[],
            ),
        )
        codex = FakeCodex()
        bot = SimpleNamespace(
            user=SimpleNamespace(id=99),
            codex_access=CodexAccess(True, 10, 20, frozenset({30})),
            codex=codex,
            server_activity=None,
            _send_ai_answer=AsyncMock(),
        )

        with patch("src.bot.read_image_attachments", AsyncMock(return_value=())):
            await HoroBot.on_message(bot, message)

        self.assertEqual(codex.calls[0][2], "follow up")
        bot._send_ai_answer.assert_awaited_once()

    async def test_reply_inherits_supported_image_from_bot_message(self):
        attachment = SimpleNamespace(
            filename="one.png",
            content_type="image/png",
            size=8,
        )
        message = make_message()
        message.content = "describe"
        message.mentions = []
        message.reference = SimpleNamespace(
            channel_id=20,
            message_id=55,
            resolved=SimpleNamespace(
                author=SimpleNamespace(id=99),
                attachments=[attachment],
            ),
        )
        codex = FakeCodex()
        bot = SimpleNamespace(
            user=SimpleNamespace(id=99),
            codex_access=CodexAccess(True, 10, 20, frozenset({30})),
            codex=codex,
            server_activity=None,
            _send_ai_answer=AsyncMock(),
        )
        image = "data:image/png;base64,iVBORw0KGgo="

        with patch(
            "src.bot.read_image_attachments",
            AsyncMock(return_value=(image,)),
        ):
            await HoroBot.on_message(bot, message)

        self.assertEqual(codex.calls[0][3], (image,))

    async def test_cooldown_and_bridge_error_never_fallback(self):
        for codex, expected in (
            (FakeCodex(allowed=False), "稍候"),
            (FakeCodex(error=CodexBridgeError("timeout")), "逾時"),
        ):
            with self.subTest(expected=expected):
                message = make_message()
                bot = SimpleNamespace(
                    user=SimpleNamespace(id=99),
                    codex_access=CodexAccess(True, 10, 20, frozenset({30})),
                    codex=codex,
                    server_activity=None,
                    _send_ai_answer=AsyncMock(),
                )

                with patch(
                    "src.bot.read_image_attachments",
                    AsyncMock(return_value=()),
                ):
                    await HoroBot.on_message(bot, message)

                self.assertEqual(codex.calls, [])
                self.assertIn(expected, message.reply.await_args.args[0])
                bot._send_ai_answer.assert_not_awaited()

    async def test_non_allowlisted_mention_never_calls_codex(self):
        message = make_message(user_id=31)
        codex = FakeCodex()
        bot = SimpleNamespace(
            user=SimpleNamespace(id=99),
            codex_access=CodexAccess(True, 10, 20, frozenset({30})),
            codex=codex,
            server_activity=None,
            _send_ai_answer=AsyncMock(),
        )

        await HoroBot.on_message(bot, message)

        self.assertEqual(codex.calls, [])
        message.reply.assert_awaited_once()
        self.assertIn("未對此身分組或頻道開放", message.reply.await_args.args[0])


class CodexArchiveRoutingTest(unittest.IsolatedAsyncioTestCase):
    async def test_thread_delete_archives_matching_conversation(self):
        codex = SimpleNamespace(archive_scope=AsyncMock())
        bot = SimpleNamespace(codex=codex, server_activity=None)
        payload = SimpleNamespace(guild_id=10, thread_id=20)

        await HoroBot.on_raw_thread_delete(bot, payload)

        codex.archive_scope.assert_awaited_once_with(10, 20)

    async def test_channel_delete_archives_matching_conversation(self):
        codex = SimpleNamespace(archive_scope=AsyncMock())
        bot = SimpleNamespace(
            codex=codex,
            calendar=SimpleNamespace(handle_channel_delete=lambda *_args: None),
            temp_voice=None,
            temp_voice_enabled=False,
        )
        channel = SimpleNamespace(id=20, guild=SimpleNamespace(id=10))

        await HoroBot.on_guild_channel_delete(bot, channel)

        codex.archive_scope.assert_awaited_once_with(10, 20)

    async def test_guild_remove_archives_all_guild_conversations(self):
        codex = SimpleNamespace(archive_scope=AsyncMock())
        bot = SimpleNamespace(
            codex=codex,
            server_activity=None,
            calendar=SimpleNamespace(delete_guild=lambda _guild_id: None),
            temp_voice=None,
            temp_voice_enabled=False,
        )

        await HoroBot.on_guild_remove(bot, SimpleNamespace(id=10))

        codex.archive_scope.assert_awaited_once_with(10)


if __name__ == "__main__":
    unittest.main()

from types import SimpleNamespace
import unittest
from unittest.mock import ANY, AsyncMock, patch

import discord

from src.bot import HoroBot
from src.ai.discord import codex_conversation_key_for_message, codex_error_text
from src.ai.access import CodexAccess
from src.ai.client import CodexBridgeClient
from src.ai.protocol import CodexBridgeError
from tests.support.access import configured_access


class Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb):
        return False


class FakeCodex(CodexBridgeClient):
    def __init__(self, reply="answer", *, allowed=True, error=None):
        super().__init__("http://codex:8765", "a" * 64, cooldown_seconds=0)
        self.reply = reply
        self.allowed = allowed
        self.error = error
        self.calls = []

    def try_start_request(self, _user_id):
        return self.allowed

    async def chat(self, key, text, images):
        if self.error is not None:
            raise self.error
        self.calls.append((key, text, images))
        return self.reply


def make_message(
    *,
    user_id=30,
    role_ids=(70,),
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
            display_name="Test User",
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
    message.guild.fetch_member = AsyncMock(return_value=message.author)
    return message


class CodexBotHelpersTest(unittest.TestCase):
    def test_allowlisted_channel_and_thread_build_expected_keys(self):
        access = configured_access(True, 10, channel_ids=(20,))
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

    def test_scope_rejects_dm_wrong_parent_and_missing_role(self):
        access = configured_access(True, 10, channel_ids=(20,))
        wrong_channel = make_message()
        wrong_channel.channel.id = 22
        wrong_user = make_message(user_id=31, role_ids=())
        direct_message = make_message()
        direct_message.guild = None

        self.assertIsNone(codex_conversation_key_for_message(wrong_channel, access))
        self.assertIsNone(codex_conversation_key_for_message(wrong_user, access))
        self.assertIsNone(codex_conversation_key_for_message(direct_message, access))

    def test_roles_require_membership_without_admin_bypass(self):
        access = configured_access(True, 10, channel_ids=(20,))
        access.set_roles(10, frozenset({70, 80}))
        allowed = make_message(user_id=31, role_ids=(60, 80))
        no_matching_role = make_message(user_id=30, role_ids=())
        administrator = make_message(user_id=32, administrator=True, role_ids=())

        self.assertEqual(
            codex_conversation_key_for_message(allowed, access),
            "guild:10:channel:20:user:31",
        )
        self.assertIsNone(codex_conversation_key_for_message(no_matching_role, access))
        self.assertIsNone(codex_conversation_key_for_message(administrator, access))

    def test_role_mode_fails_closed_when_member_roles_are_missing(self):
        access = configured_access(True, 10, channel_ids=(20,))
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
            codex_access=configured_access(True, 10, channel_ids=(20,)),
            codex=codex,
            ai_text_display_enabled=False,
            intents=SimpleNamespace(members=False),
        )

        with patch("src.ai.discord.read_image_attachments", AsyncMock(return_value=())):
            await HoroBot.on_message(bot, message)

        self.assertEqual(
            codex.calls,
            [("guild:10:channel:20:user:30", "hello", ())],
        )
        message.reply.assert_awaited_once_with("answer", mention_author=False, allowed_mentions=ANY)

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
            codex_access=configured_access(True, 10, channel_ids=(20,)),
            codex=codex,
            ai_text_display_enabled=False,
            intents=SimpleNamespace(members=False),
        )

        with patch("src.ai.discord.read_image_attachments", AsyncMock(return_value=())):
            await HoroBot.on_message(bot, message)

        self.assertEqual(codex.calls[0][1], "follow up")
        message.reply.assert_awaited_once_with("answer", mention_author=False, allowed_mentions=ANY)

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
            codex_access=configured_access(True, 10, channel_ids=(20,)),
            codex=codex,
            ai_text_display_enabled=False,
            intents=SimpleNamespace(members=False),
        )
        image = "data:image/png;base64,iVBORw0KGgo="

        with patch(
            "src.ai.discord.read_image_attachments",
            AsyncMock(return_value=(image,)),
        ):
            await HoroBot.on_message(bot, message)

        self.assertEqual(codex.calls[0][2], (image,))

    async def test_cooldown_and_bridge_error_never_fallback(self):
        for codex, expected in (
            (FakeCodex(allowed=False), "稍候"),
            (FakeCodex(error=CodexBridgeError("timeout")), "逾時"),
        ):
            with self.subTest(expected=expected):
                message = make_message()
                bot = SimpleNamespace(
                    user=SimpleNamespace(id=99),
                    codex_access=configured_access(True, 10, channel_ids=(20,)),
                    codex=codex,
                    ai_text_display_enabled=False,
                    intents=SimpleNamespace(members=False),
                )

                with patch(
                    "src.ai.discord.read_image_attachments",
                    AsyncMock(return_value=()),
                ):
                    await HoroBot.on_message(bot, message)

                self.assertEqual(codex.calls, [])
                self.assertIn(expected, message.reply.await_args.args[0])
                self.assertNotEqual(message.reply.await_args.args[0], "answer")

    async def test_non_allowlisted_mention_never_calls_codex(self):
        message = make_message(user_id=31, role_ids=())
        codex = FakeCodex()
        bot = SimpleNamespace(
            user=SimpleNamespace(id=99),
            codex_access=configured_access(True, 10, channel_ids=(20,)),
            codex=codex,
            ai_text_display_enabled=False,
            intents=SimpleNamespace(members=False),
        )

        await HoroBot.on_message(bot, message)

        self.assertEqual(codex.calls, [])
        message.reply.assert_awaited_once()
        self.assertIn("未對此身分組或頻道開放", message.reply.await_args.args[0])


class CodexArchiveRoutingTest(unittest.IsolatedAsyncioTestCase):
    async def test_thread_delete_archives_matching_conversation(self):
        codex = SimpleNamespace(archive_scope=AsyncMock())
        bot = SimpleNamespace(codex=codex)
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
            calendar=SimpleNamespace(delete_guild=lambda _guild_id: None),
            temp_voice=None,
            temp_voice_enabled=False,
        )

        await HoroBot.on_guild_remove(bot, SimpleNamespace(id=10))

        codex.archive_scope.assert_awaited_once_with(10, None)


class CodexMemberIntentTest(unittest.IsolatedAsyncioTestCase):
    async def test_member_intent_tracks_ai_even_without_role_configuration(self):
        for enabled in (False, True):
            for roles_configured in (False, True):
                with self.subTest(enabled=enabled, roles_configured=roles_configured):
                    access = configured_access(enabled, 10, channel_ids=(20,), role_ids=())
                    if roles_configured:
                        access.set_roles(10, frozenset({70}))
                    bot = HoroBot(
                        SimpleNamespace(close=AsyncMock()),
                        access,
                        SimpleNamespace(),
                        SimpleNamespace(close=AsyncMock()),
                        SimpleNamespace(close=AsyncMock()),
                    )
                    try:
                        self.assertEqual(bot.intents.members, enabled)
                        self.assertTrue(bot.intents.message_content)
                        self.assertTrue(bot.intents.voice_states)
                    finally:
                        await bot.close()


class RetainedEventRoutingTest(unittest.IsolatedAsyncioTestCase):
    async def test_role_revocation_still_cancels_without_monitor(self):
        access = configured_access(True, 10, channel_ids=(20,), role_ids=())
        access.set_roles(10, frozenset({70}))
        codex = SimpleNamespace(cancel_member=AsyncMock())
        bot = SimpleNamespace(codex_access=access, codex=codex)
        before = SimpleNamespace(id=30, guild=SimpleNamespace(id=10), roles=[SimpleNamespace(id=70)])
        after = SimpleNamespace(id=30, guild=before.guild, roles=[])
        await HoroBot.on_member_update(bot, before, after)
        codex.cancel_member.assert_awaited_once_with(10, 30)

    async def test_remaining_allowed_role_does_not_cancel_member(self):
        access = configured_access(True, 10, channel_ids=(20,), role_ids=())
        access.set_roles(10, frozenset({70, 80}))
        codex = SimpleNamespace(cancel_member=AsyncMock())
        bot = SimpleNamespace(codex_access=access, codex=codex)
        before = SimpleNamespace(id=30, guild=SimpleNamespace(id=10), roles=[SimpleNamespace(id=70)])
        after = SimpleNamespace(id=30, guild=before.guild, roles=[SimpleNamespace(id=80)])
        await HoroBot.on_member_update(bot, before, after)
        codex.cancel_member.assert_not_awaited()

    async def test_board_delete_still_routes_without_monitor(self):
        calendar = SimpleNamespace(handle_board_message_delete=AsyncMock())
        bot = SimpleNamespace(calendar=calendar)
        payload = SimpleNamespace(guild_id=10, channel_id=20, message_id=30)
        await HoroBot.on_raw_message_delete(bot, payload)
        calendar.handle_board_message_delete.assert_awaited_once_with(10, 20, 30)

    async def test_voice_update_still_routes_without_monitor(self):
        voice = SimpleNamespace(handle_voice_state_update=AsyncMock())
        bot = SimpleNamespace(temp_voice_enabled=True, temp_voice=voice)
        member, before, after = object(), object(), object()
        await HoroBot.on_voice_state_update(bot, member, before, after)
        voice.handle_voice_state_update.assert_awaited_once_with(member, before, after)
        voice.handle_voice_state_update.reset_mock()
        bot.temp_voice_enabled = False
        await HoroBot.on_voice_state_update(bot, member, before, after)
        voice.handle_voice_state_update.assert_not_awaited()

    async def test_guild_join_preserves_scoped_voice_reconcile(self):
        voice = SimpleNamespace(reconcile=AsyncMock())
        bot = SimpleNamespace(temp_voice_enabled=True, temp_voice=voice)
        guild = SimpleNamespace(id=10)
        await HoroBot.on_guild_join(bot, guild)
        voice.reconcile.assert_awaited_once_with([guild], prune_absent=False)

    async def test_guild_cleanup_keeps_other_services_when_calendar_fails(self):
        def fail_calendar(_guild_id):
            raise RuntimeError("private state detail")
        calendar = SimpleNamespace(delete_guild=fail_calendar)
        voice = SimpleNamespace(delete_guild=AsyncMock())
        codex = SimpleNamespace(archive_scope=AsyncMock())
        bot = SimpleNamespace(calendar=calendar, temp_voice=voice, temp_voice_enabled=True, codex=codex)
        with self.assertLogs(level="ERROR") as logs:
            await HoroBot.on_guild_remove(bot, SimpleNamespace(id=10))
        voice.delete_guild.assert_awaited_once_with(10)
        codex.archive_scope.assert_awaited_once_with(10, None)
        self.assertNotIn("private state detail", repr(logs.output))

    async def test_scheduled_event_updates_still_refresh_bound_calendar(self):
        guild = SimpleNamespace(id=10)
        calendar = SimpleNamespace(has_binding=lambda _guild_id: True, refresh_guild=AsyncMock())
        bot = SimpleNamespace(calendar=calendar, get_guild=lambda _guild_id: guild)
        event = SimpleNamespace(guild_id=10)
        await HoroBot.on_scheduled_event_create(bot, event)
        await HoroBot.on_scheduled_event_update(bot, event, event)
        await HoroBot.on_scheduled_event_delete(bot, event)
        self.assertEqual(calendar.refresh_guild.await_count, 3)
        for call in calendar.refresh_guild.await_args_list:
            self.assertEqual(call.args, (guild,))


if __name__ == "__main__":
    unittest.main()

import asyncio
import contextlib
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from src.ai import AiSettings, ModelError, ModelReply
from src.ai.adapters.discord.errors import codex_error_text
from src.ai.adapters.discord.messages import AiMessages
from src.ai.adapters.media.frames import Frames
from src.ai.adapters.media.images import MediaReader
from src.ai.domain.conversation import MAX_PROMPT_CHARACTERS
from src.ai.domain.prompts import CHAT_INSTRUCTIONS
from src.core.adapters.discord.live import LiveGuild
from src.core.app.supervisor import PRESENTATION
from tests.support import testing
from tests.support.ai import FakeActions, FakeMedia, FakeModel, Games, MemoryAiStore

BOT, GUILD, CHANNEL, USER, ROLE = 10, 20, 30, 40, 50
STICKER_URL = "https://cdn.discordapp.com/stickers/7.png"
TIP = "第一次來？跟我說「說明」看看我能做什麼"
text = testing.text


def attachment(id: int, filename: str, content_type: str) -> SimpleNamespace:
    url = f"https://cdn.discordapp.com/attachments/1/{id}/{filename}"
    return SimpleNamespace(id=id, filename=filename, content_type=content_type, url=url)


def make_message(content: str = f"<@{BOT}> 你好", *, mention: bool = True, reference=None) -> SimpleNamespace:
    author = SimpleNamespace(id=USER, bot=False, roles=[SimpleNamespace(id=ROLE)])
    progress = SimpleNamespace(edit=AsyncMock(), delete=AsyncMock())
    channel = SimpleNamespace(
        id=CHANNEL, typing=contextlib.nullcontext, send=AsyncMock(), fetch_message=AsyncMock(),
        permissions_for=lambda member: discord.Permissions(view_channel=True, send_messages=True),
    )
    guild = SimpleNamespace(
        id=GUILD, unavailable=False, me=SimpleNamespace(display_name="霍蘿"),
        get_member=lambda user_id: author,
        get_channel_or_thread=lambda channel_id: channel if channel_id == CHANNEL else None,
    )
    return SimpleNamespace(
        id=900, author=author, webhook_id=None, content=content, guild=guild, channel=channel,
        mentions=[SimpleNamespace(id=BOT)] if mention else [], reference=reference,
        attachments=[], embeds=[], stickers=[], components=[],
        reply=AsyncMock(return_value=progress), to_reference=lambda **kwargs: SimpleNamespace(**kwargs),
        progress=progress,
    )


class Harness:
    def __init__(self, message: SimpleNamespace, **store: object) -> None:
        self.message = message
        self.model = FakeModel()
        self.store = MemoryAiStore(**({"channel_ids": (CHANNEL,), "role_ids": (ROLE,)} | store))
        self.settings = AiSettings(self.store, self.model, GUILD)
        self.actions = FakeActions()
        self.media = FakeMedia()
        live = LiveGuild(SimpleNamespace(get_guild=lambda guild_id: message.guild), GUILD, lambda: False)
        self.messages = AiMessages(live, self.settings, self.model, self.actions, Games(99), self.media,
                                   cleanups=testing.group(PRESENTATION))

    async def send(self, message: SimpleNamespace | None = None) -> None:
        await self.messages.handle(message or self.message, BOT)

    def sent(self) -> str:
        return text(self.message.channel.send.await_args.kwargs["view"])

    def answered(self) -> str:
        return self.message.channel.send.await_args.args[0]


class EntryTests(unittest.IsolatedAsyncioTestCase):
    async def test_unmentioned_message_is_ignored(self) -> None:
        harness = Harness(make_message("隨便聊聊", mention=False))
        await harness.send()
        harness.message.reply.assert_not_awaited()
        harness.model.try_start_request.assert_not_called()

    async def test_plain_message_reads_no_settings(self) -> None:
        harness = Harness(make_message("隨便聊聊", mention=False))
        harness.store.failure = AssertionError("settings read")
        await harness.send()

    async def test_bots_and_webhooks_are_ignored(self) -> None:
        harness = Harness(make_message())
        harness.message.author.bot = True
        await harness.send()
        harness.message.author.bot, harness.message.webhook_id = False, 5
        await harness.send()
        harness.message.channel.send.assert_not_awaited()
        harness.model.try_start_request.assert_not_called()

    async def test_disabled_ai_ignores_mention(self) -> None:
        harness = Harness(make_message(), enabled=False)
        await harness.send()
        harness.message.channel.send.assert_not_awaited()
        harness.model.try_start_request.assert_not_called()

    async def test_outside_allowlist_names_up_to_five_ai_channels(self) -> None:
        harness = Harness(make_message(), channel_ids=(107, 101, 103, 102, 106, 104, 105))
        await harness.send()
        harness.message.channel.send.assert_awaited_once()
        sent = harness.message.channel.send.await_args.kwargs
        self.assertIn("這個頻道不能找霍蘿聊天，請到 <#101>、<#102>、<#103>、<#104>、<#105> 再 @霍蘿。", text(sent["view"]))
        self.assertFalse(sent["reference"].fail_if_not_exists)
        harness.model.try_start_request.assert_not_called()

    async def test_missing_role_and_no_ai_channel_get_their_own_notice(self) -> None:
        for store, expected in (
            ({"role_ids": (51,)}, "你目前沒有使用霍蘿 AI 的身分組，需要的話請找管理員。"),
            ({"channel_ids": ()}, "霍蘿 目前沒有在這個頻道或你的身分組開放。"),
        ):
            with self.subTest(store=store):
                harness = Harness(make_message(), **store)
                await harness.send()
                self.assertIn(expected, harness.sent())
                self.assertNotIn("<#", harness.sent())

    async def test_reply_ping_outside_allowlist_stays_silent(self) -> None:
        reference = SimpleNamespace(message_id=1, channel_id=CHANNEL, resolved=None)
        harness = Harness(make_message("謝謝", reference=reference), channel_ids=(31,))
        await harness.send()
        harness.message.channel.send.assert_not_awaited()
        harness.model.try_start_request.assert_not_called()

    async def test_reply_to_someone_else_is_ignored(self) -> None:
        other = SimpleNamespace(author=SimpleNamespace(id=USER + 1), content="別人", attachments=[])
        reference = SimpleNamespace(message_id=1, channel_id=CHANNEL, resolved=other)
        harness = Harness(make_message("謝謝", mention=False, reference=reference))
        await harness.send()
        harness.message.channel.send.assert_not_awaited()
        harness.model.try_start_request.assert_not_called()

    async def test_suspended_allowlist_gets_busy_notice(self) -> None:
        harness = Harness(make_message())
        harness.settings.suspend(frozenset({CHANNEL}))
        await harness.send()
        self.assertIn(codex_error_text("busy", "霍蘿"), harness.sent())

    async def test_cooldown_notice(self) -> None:
        harness = Harness(make_message())
        harness.model.try_start_request.return_value = False
        await harness.send()
        self.assertIn("你發問得太快了", harness.sent())
        harness.model.chat.assert_not_awaited()

    async def test_help_words_reply_the_help_card_without_ai(self) -> None:
        for words in ("說明", "你會什麼？", "你會什麼?"):
            harness = Harness(make_message(f"<@{BOT}>  {words} "))
            await harness.send()
            self.assertIn("<#99>", harness.sent())
            harness.model.try_start_request.assert_not_called()
            harness.model.chat.assert_not_awaited()


class AnswerTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_sends_answer_as_new_reply_and_deletes_progress_card(self) -> None:
        harness = Harness(make_message())
        await harness.send()
        harness.message.reply.assert_awaited_once()
        self.assertEqual(harness.model.chat.await_args.args[1:3], ("你好", ()))
        self.assertEqual(harness.model.chat.await_args.kwargs["instructions"], CHAT_INSTRUCTIONS)
        self.assertEqual(harness.model.accepted, [f"guild:{GUILD}:channel:{CHANNEL}:user:{USER}"])
        sent = harness.message.channel.send.await_args
        self.assertTrue(sent.args[0].endswith("最終答案"))
        self.assertNotIn("view", sent.kwargs)
        self.assertFalse(sent.kwargs["reference"].fail_if_not_exists)
        harness.message.progress.edit.assert_not_awaited()
        harness.message.progress.delete.assert_awaited_once()

    async def test_answer_notifies_only_the_asker(self) -> None:
        harness = Harness(make_message())
        await harness.send()
        sent = harness.message.channel.send.await_args
        self.assertEqual(sent.args[:1], (f"-# ℹ️ {TIP}\n最終答案",))
        self.assertEqual(discord.AllowedMentions.none().merge(sent.kwargs["allowed_mentions"]).to_dict(),
                         {"parse": [], "replied_user": True})

    async def test_notes_share_one_line_and_the_tip_comes_once(self) -> None:
        harness = Harness(make_message())
        harness.model.chat.return_value = ModelReply("最終答案", restarted=True)
        await harness.send()
        self.assertEqual(harness.answered(), f"-# ℹ️ 已開始新的對話（先前的無法接續） · {TIP}\n最終答案")
        await harness.send()
        self.assertEqual(harness.answered(), "-# ℹ️ 已開始新的對話（先前的無法接續）\n最終答案")
        harness.model.chat.return_value = ModelReply("最終答案")
        await harness.send()
        self.assertEqual(harness.answered(), "最終答案")

    async def test_typing_failure_still_sends_answer(self) -> None:
        harness = Harness(make_message())
        response = MagicMock(status=500, reason="down")
        harness.message.channel.typing = MagicMock(side_effect=discord.HTTPException(response, "down"))
        await harness.send()
        self.assertIn("最終答案", harness.answered())

    async def test_progress_card_timeout_still_sends_answer(self) -> None:
        harness = Harness(make_message())
        harness.message.reply.side_effect = TimeoutError
        with self.assertLogs(level="ERROR"):
            await harness.send()
        self.assertIn("最終答案", harness.answered())
        harness.message.progress.edit.assert_not_awaited()

    async def test_chat_may_outlive_work_deadline_before_job_deadline(self) -> None:
        harness = Harness(make_message())
        harness.model.work_seconds = 0.05

        async def slow(*args, **kwargs):
            await asyncio.sleep(0.2)
            return ModelReply("慢慢來的答案")

        harness.model.chat.side_effect = slow
        await harness.send()
        self.assertIn("慢慢來的答案", harness.answered())

    async def test_reply_to_bot_without_ping_carries_its_text_and_media(self) -> None:
        referenced = SimpleNamespace(
            author=SimpleNamespace(id=BOT), content="被回覆文字", attachments=[], embeds=[],
            stickers=[SimpleNamespace(id=7, url=STICKER_URL, format=SimpleNamespace(name="png"))], components=[],
        )
        reference = SimpleNamespace(channel_id=CHANNEL, resolved=referenced, message_id=99)
        harness = Harness(make_message("這是什麼", mention=False, reference=reference))
        await harness.send()
        _key, prompt, images = harness.model.chat.await_args.args
        self.assertEqual(images, (f"img:{STICKER_URL}",))
        self.assertTrue(prompt.startswith("【被回覆的訊息】\n被回覆文字"))
        self.assertIn("這是什麼", prompt)
        self.assertIn("最終答案", harness.answered())

    async def test_media_deadline_is_bounded_by_the_work_deadline(self) -> None:
        harness = Harness(make_message())
        harness.model.work_seconds = 0.5
        await harness.send()
        deadline = harness.media.read.await_args.kwargs["deadline"]
        self.assertLessEqual(deadline, harness.model.job.work_deadline)


class FailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_busy_service_turns_progress_card_into_error(self) -> None:
        harness = Harness(make_message())
        harness.model.admission_error = ModelError("busy")
        await harness.send()
        self.assertIn(codex_error_text("busy", "霍蘿"), text(harness.message.progress.edit.await_args.kwargs["view"]))
        harness.message.progress.delete.assert_not_awaited()
        harness.model.chat.assert_not_awaited()

    async def test_failed_error_card_edit_still_deletes_card(self) -> None:
        harness = Harness(make_message())
        harness.model.admission_error = ModelError("busy")
        harness.message.progress.edit.side_effect = discord.HTTPException(MagicMock(status=500), "down")
        with self.assertLogs(level="ERROR") as logs:
            await harness.send()
        self.assertIn("Failed to send Discord AI status reply.", "\n".join(logs.output))
        harness.message.progress.delete.assert_awaited_once()

    async def test_busy_without_progress_card_replies_in_channel(self) -> None:
        harness = Harness(make_message())
        harness.model.admission_error = ModelError("busy")
        harness.message.reply.side_effect = TimeoutError
        with self.assertLogs(level="ERROR"):
            await harness.send()
        sent = harness.message.channel.send.await_args.kwargs
        self.assertIn(codex_error_text("busy", "霍蘿"), text(sent["view"]))
        self.assertFalse(sent["reference"].fail_if_not_exists)
        harness.message.progress.edit.assert_not_awaited()

    async def test_unreadable_model_settings_name_the_configuration(self) -> None:
        harness = Harness(make_message(), models={"primary": "broken"})
        await harness.send()
        self.assertIn(codex_error_text("model_configuration_invalid", "霍蘿"),
                      text(harness.message.progress.edit.await_args.kwargs["view"]))
        harness.model.chat.assert_not_awaited()

    async def test_revoked_mid_request_deletes_progress_card(self) -> None:
        harness = Harness(make_message())

        async def revoke(*args, **kwargs):
            harness.settings.suspend(frozenset({CHANNEL}))
            return ModelReply("不該送出")

        harness.model.chat.side_effect = revoke
        await harness.send()
        harness.message.progress.edit.assert_not_awaited()
        harness.message.progress.delete.assert_awaited_once()
        harness.message.channel.send.assert_not_awaited()

    async def test_overlong_question_notice(self) -> None:
        harness = Harness(make_message(f"<@{BOT}> " + "字" * (MAX_PROMPT_CHARACTERS + 1)))
        await harness.send()
        self.assertIn(f"問題最多 {MAX_PROMPT_CHARACTERS:,} 個字元", text(harness.message.progress.edit.await_args.kwargs["view"]))
        harness.model.structured.assert_not_awaited()
        harness.model.chat.assert_not_awaited()

    async def test_cancel_deletes_progress_card_without_raising(self) -> None:
        harness = Harness(make_message())
        started = asyncio.Event()

        async def hang(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        harness.model.chat.side_effect = hang
        task = asyncio.create_task(harness.send())
        await started.wait()
        task.cancel()
        self.assertIsNone(await task)
        harness.message.progress.delete.assert_awaited_once()
        harness.message.progress.edit.assert_not_awaited()

    async def test_unexpected_error_turns_progress_card_into_error(self) -> None:
        harness = Harness(make_message())
        harness.model.chat.side_effect = RuntimeError("locked")
        with self.assertLogs(level="ERROR") as logs:
            await harness.send()
        self.assertIn("failed unexpectedly", "\n".join(logs.output))
        self.assertIn(codex_error_text("unavailable", "霍蘿"), text(harness.message.progress.edit.await_args.kwargs["view"]))
        harness.message.progress.delete.assert_not_awaited()
        harness.message.channel.send.assert_not_awaited()

    async def test_unreadable_media_is_noted_or_reported(self) -> None:
        harness = Harness(make_message())
        harness.media.read.side_effect = AsyncMock(return_value=([[]], 0, 1, 0))
        await harness.send()
        self.assertIn("另有 1 個媒體無法讀取", harness.model.chat.await_args.args[1])
        harness = Harness(make_message(f"<@{BOT}>"))
        harness.message.stickers = [SimpleNamespace(id=7, url=STICKER_URL, format=SimpleNamespace(name="png"))]
        harness.media.read.side_effect = AsyncMock(return_value=([[]], 0, 1, 0))
        await harness.send()
        self.assertIn("目前無法讀取這些媒體", text(harness.message.progress.edit.await_args.kwargs["view"]))
        harness.model.chat.assert_not_awaited()

    async def test_unsupported_image_is_noted_and_answered(self) -> None:
        harness = Harness(make_message())
        harness.message.attachments = [attachment(1, "a.svg", "image/svg+xml")]
        harness.messages = AiMessages(
            LiveGuild(SimpleNamespace(get_guild=lambda guild_id: harness.message.guild), GUILD, lambda: False),
            harness.settings, harness.model, harness.actions, Games(99), MediaReader(Frames()),
            cleanups=testing.group(PRESENTATION),
        )
        await harness.send()
        _key, prompt, images = harness.model.chat.await_args.args
        self.assertEqual(images, ())
        self.assertIn("另有 1 個媒體無法讀取", prompt)
        self.assertIn("最終答案", harness.answered())


if __name__ == "__main__":
    unittest.main()

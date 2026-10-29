import asyncio
import contextlib
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from src.ai import AiSettings, ModelError, ModelReply
from src.ai.adapters.discord.errors import codex_error_text
from src.ai.adapters.discord.messages import AiMessages
from src.ai.adapters.media.frames import Frames
from src.ai.adapters.media.images import MediaReader
from src.ai.adapters.sqlite.store import SqliteAiStore
from src.ai.domain.conversation import MAX_PROMPT_CHARACTERS
from src.ai.domain.prompts import CHAIN_NOTE, CHAT_INSTRUCTIONS
from src.core.adapters.discord.live import LiveGuild
from src.core.adapters.sqlite.db import open_db
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


def history_message(
    channel: SimpleNamespace, message_id: int, author: SimpleNamespace, content: str, parent: int | None = None,
    **fields: object,
) -> SimpleNamespace:
    reference = None if parent is None else SimpleNamespace(message_id=parent, channel_id=channel.id, resolved=None)
    return SimpleNamespace(**({
        "id": message_id, "author": author, "content": content, "channel": channel, "reference": reference,
        "attachments": [], "embeds": [], "stickers": [], "components": [],
    } | fields))


def serve_history(channel: SimpleNamespace, history: dict[int, object]) -> None:
    async def fetch(message_id: int) -> object:
        found = history[message_id]
        if isinstance(found, BaseException):
            raise found
        if found is None:
            await asyncio.Event().wait()
        return found

    channel.fetch_message = AsyncMock(side_effect=fetch)


MEMBER = SimpleNamespace(id=USER, display_name="小明")
BOT_AUTHOR = SimpleNamespace(id=BOT, display_name="舊名字")


class Harness:
    def __init__(self, message: SimpleNamespace, *, tips: object | None = None, **store: object) -> None:
        self.message = message
        self.model = FakeModel()
        self.store = MemoryAiStore(**({"channel_ids": (CHANNEL,), "role_ids": (ROLE,)} | store))
        self.settings = AiSettings(self.store, self.model, GUILD)
        self.actions = FakeActions()
        self.media = FakeMedia()
        live = LiveGuild(SimpleNamespace(get_guild=lambda guild_id: message.guild), GUILD, lambda: False)
        self.messages = AiMessages(live, self.settings, self.model, self.actions, Games(99), self.media,
                                   tips=self.store if tips is None else tips, cleanups=testing.group(PRESENTATION),
                                   confirmations=MagicMock())

    def reply_to(self, history: dict[int, object], nearest: int) -> None:
        self.message.reference = SimpleNamespace(message_id=nearest, channel_id=CHANNEL, resolved=None)
        serve_history(self.message.channel, history)

    def prompt(self) -> str:
        return self.model.chat.await_args.args[1]

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

    async def test_tip_is_remembered_across_restarts(self) -> None:
        db = open_db(":memory:")
        self.addCleanup(db.close)
        answers = []
        for _restart in range(2):
            harness = Harness(make_message(), tips=SqliteAiStore(db))
            await harness.send()
            answers.append(harness.answered())
        self.assertEqual(answers, [f"-# ℹ️ {TIP}\n最終答案", "最終答案"])

    async def test_failed_answer_keeps_the_tip_for_later(self) -> None:
        harness = Harness(make_message())
        harness.model.chat.side_effect = ModelError("unavailable")
        await harness.send()
        harness.message.channel.send.assert_not_awaited()
        harness.model.chat.side_effect = None
        await harness.send()
        self.assertEqual(harness.answered(), f"-# ℹ️ {TIP}\n最終答案")

    async def test_unreadable_tip_state_still_answers(self) -> None:
        db = open_db(":memory:")
        self.addCleanup(db.close)
        db.execute("DROP TABLE ai_tipped")
        harness = Harness(make_message(), tips=SqliteAiStore(db))
        with self.assertLogs(level="WARNING"):
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
        self.assertTrue(prompt.startswith("【被回覆的訊息】霍蘿：\n被回覆文字"))
        self.assertIn("這是什麼", prompt)
        self.assertIn("最終答案", harness.answered())

    async def test_media_deadline_is_bounded_by_the_work_deadline(self) -> None:
        harness = Harness(make_message())
        harness.model.work_seconds = 0.5
        await harness.send()
        deadline = harness.media.read.await_args.kwargs["deadline"]
        self.assertLessEqual(deadline, harness.model.job.work_deadline)


class ReplyChainTests(unittest.IsolatedAsyncioTestCase):
    def chain(self, harness: Harness, length: int) -> dict[int, object]:
        channel = harness.message.channel
        return {
            n: history_message(channel, n, BOT_AUTHOR if n % 2 == 0 else MEMBER, f"第 {n} 則", n - 1 if n > 1 else None)
            for n in range(1, length + 1)
        }

    async def test_chain_reads_five_messages_oldest_first_with_author_names(self) -> None:
        harness = Harness(make_message(f"<@{BOT}> 然後呢"))
        harness.reply_to(self.chain(harness, 7), 7)
        await harness.send()
        self.assertEqual(
            harness.prompt(),
            f"{CHAIN_NOTE}\n\n【回覆鏈 1】小明：\n第 3 則\n\n【回覆鏈 2】霍蘿：\n第 4 則\n\n【回覆鏈 3】小明：\n第 5 則\n\n"
            "【回覆鏈 4】霍蘿：\n第 6 則\n\n【被回覆的訊息】小明：\n第 7 則\n\n【本次問題】\n然後呢",
        )
        self.assertEqual([call.args[0] for call in harness.message.channel.fetch_message.await_args_list],
                         [7, 6, 5, 4, 3])

    async def test_chain_stops_quietly_where_history_cannot_be_read(self) -> None:
        response = MagicMock(status=404, reason="gone")
        breaks = {
            "已刪除": lambda older: setattr(older.reference, "resolved", discord.DeletedReferencedMessage(
                discord.MessageReference(message_id=1, channel_id=CHANNEL))),
            "跨頻道": lambda older: setattr(older.reference, "channel_id", CHANNEL + 1),
            "HTTP 錯誤": lambda older: history.__setitem__(1, discord.NotFound(response, "gone")),
            "逾時": lambda older: history.__setitem__(1, None),
        }
        for name, cut in breaks.items():
            with self.subTest(name), patch("src.ai.adapters.discord.context.REFERENCE_TIMEOUT_SECONDS", 0.05):
                harness = Harness(make_message(f"<@{BOT}> 然後呢"))
                history = self.chain(harness, 3)
                cut(history[2])
                harness.reply_to(history, 3)
                await harness.send()
                self.assertEqual(
                    harness.prompt(),
                    f"{CHAIN_NOTE}\n\n【回覆鏈 1】霍蘿：\n第 2 則\n\n【被回覆的訊息】小明：\n第 3 則\n\n【本次問題】\n然後呢",
                )
                self.assertTrue(harness.answered().endswith("最終答案"))
                harness.message.progress.edit.assert_not_awaited()

    async def test_chain_reading_stops_at_the_context_deadline(self) -> None:
        harness = Harness(make_message(f"<@{BOT}> 然後呢"))
        history = self.chain(harness, 3)
        history[1] = None
        harness.reply_to(history, 3)
        loop = asyncio.get_running_loop()
        started = loop.time()
        with patch("src.ai.adapters.discord.request.CONTEXT_TIMEOUT_SECONDS", 0.05):
            await harness.send()
        self.assertLess(loop.time() - started, 1)
        self.assertTrue(harness.prompt().startswith(f"{CHAIN_NOTE}\n\n【回覆鏈 1】霍蘿：\n第 2 則\n\n【被回覆的訊息】小明：\n第 3 則"))
        self.assertLess(harness.media.read.await_args.kwargs["deadline"], started + 1)

    async def test_long_reply_ends_the_chain_without_more_reads(self) -> None:
        harness = Harness(make_message(f"<@{BOT}> 然後呢"))
        history = self.chain(harness, 2)
        history[2] = history_message(harness.message.channel, 2, MEMBER, "長" * MAX_PROMPT_CHARACTERS, 1)
        harness.reply_to(history, 2)
        await harness.send()
        harness.message.channel.fetch_message.assert_awaited_once_with(2)
        self.assertTrue(harness.prompt().startswith("【被回覆的訊息】小明：\n長"))

    async def test_only_the_replied_message_and_the_question_lend_media(self) -> None:
        harness = Harness(make_message(f"<@{BOT}> 看圖"))
        older_sticker = SimpleNamespace(id=8, url="https://cdn.discordapp.com/stickers/8.png",
                                        format=SimpleNamespace(name="png"))
        nearest_sticker = SimpleNamespace(id=7, url=STICKER_URL, format=SimpleNamespace(name="png"))
        channel = harness.message.channel
        history = {
            1: history_message(channel, 1, MEMBER, "舊圖", stickers=[older_sticker]),
            2: history_message(channel, 2, BOT_AUTHOR, "新圖", 1, stickers=[nearest_sticker]),
        }
        harness.reply_to(history, 2)
        await harness.send()
        self.assertEqual(harness.media.read.await_args.args[0], [[("media", STICKER_URL)], []])
        self.assertEqual(harness.model.chat.await_args.args[2], (f"img:{STICKER_URL}",))
        self.assertIn("【回覆鏈 1】小明：\n舊圖", harness.prompt())

    async def test_unreadable_replied_message_is_reported(self) -> None:
        harness = Harness(make_message(f"<@{BOT}> 然後呢"))
        harness.reply_to({7: discord.NotFound(MagicMock(status=404, reason="gone"), "gone")}, 7)
        await harness.send()
        self.assertIn("目前無法讀取被回覆的訊息", text(harness.message.progress.edit.await_args.kwargs["view"]))
        harness.model.chat.assert_not_awaited()


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
            tips=harness.store, cleanups=testing.group(PRESENTATION), confirmations=MagicMock(),
        )
        await harness.send()
        _key, prompt, images = harness.model.chat.await_args.args
        self.assertEqual(images, ())
        self.assertIn("另有 1 個媒體無法讀取", prompt)
        self.assertIn("最終答案", harness.answered())


if __name__ == "__main__":
    unittest.main()

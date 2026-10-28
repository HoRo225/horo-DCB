from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, NonCallableMock, patch

import discord

from src.ai.cog import AiCog
from src.ai.background import DATA_RULE, PERSONA, ask, ask_once, clean_reply
from src.ai.discord import TIPPED, handle_message
from src.ai.formatting import codex_error_text
from src.codex.instructions import CHAT_INSTRUCTIONS
from src.codex.model_settings import DEFAULT_MODEL_SETTINGS
from src.codex.protocol import MAX_PROMPT_CHARACTERS, CodexError, CodexChatReply
from src.core.adapters.discord.live import LiveGuild
from src.core.app.supervisor import REQUEST
from src.testing import AI_CLEANUPS, group, text as _text

BOT, GUILD, CHANNEL, USER = 10, 20, 30, 40
STICKER_URL = "https://cdn.discordapp.com/stickers/7.png"
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 8


def _attachment(id: int, filename: str, content_type: str) -> SimpleNamespace:
    url = f"https://cdn.discordapp.com/attachments/1/{id}/{filename}"
    return SimpleNamespace(id=id, filename=filename, content_type=content_type, url=url)


class _Access:
    def __init__(self, denial: str | None = None) -> None:
        self.denial = denial
        self.enabled = True
        self.generation = 1
        self.current = True
        self.role_ids = frozenset()
        self.text_display = True
        self.models = DEFAULT_MODEL_SETTINGS
        self.channel_ids = frozenset()

    def denial_reason(self, guild_id, channel_id, role_ids):
        return self.denial

    def channel_generation(self, channel_id):
        return 0

    def is_current(self, generation, channel_id=None, channel_generation=0):
        return self.current and generation == self.generation


class _Codex:
    def __init__(self, *, allow: bool = True, admission_error=None, work_seconds=5.0) -> None:
        self.try_start_request = MagicMock(return_value=allow)
        self.chat = AsyncMock(return_value=CodexChatReply("最終答案"))
        self.generate_once = AsyncMock(return_value=CodexChatReply("最終答案"))
        self.quota_full = MagicMock(return_value=False)
        self.admission_error = admission_error
        self.work_seconds = work_seconds
        self.job = None

    @contextlib.asynccontextmanager
    async def accepted_request(self, key, **kwargs):
        if self.admission_error is not None:
            raise self.admission_error
        now = asyncio.get_running_loop().time()
        self.job = SimpleNamespace(
            deadline=now + 5,
            work_deadline=now + self.work_seconds,
            current=True,
            started_at=now,
            accepted_at=now,
        )
        yield self.job


def _message(content: str = f"<@{BOT}> 你好", *, mention: bool = True, reference=None):
    author = SimpleNamespace(id=USER, bot=False, roles=[])
    progress = SimpleNamespace(edit=AsyncMock(), delete=AsyncMock())
    channel = SimpleNamespace(
        id=CHANNEL,
        typing=contextlib.nullcontext,
        send=AsyncMock(),
        fetch_message=AsyncMock(),
        permissions_for=lambda member: discord.Permissions(view_channel=True, send_messages=True),
    )
    guild = SimpleNamespace(
        id=GUILD,
        unavailable=False,
        me=SimpleNamespace(display_name="霍蘿"),
        get_member=lambda user_id: author,
        get_channel_or_thread=lambda channel_id: channel if channel_id == CHANNEL else None,
    )
    return SimpleNamespace(
        author=author,
        webhook_id=None,
        content=content,
        guild=guild,
        channel=channel,
        mentions=[SimpleNamespace(id=BOT)] if mention else [],
        reference=reference,
        attachments=[],
        embeds=[],
        stickers=[],
        components=[],
        reply=AsyncMock(return_value=progress),
        to_reference=lambda **kwargs: SimpleNamespace(**kwargs),
        progress=progress,
    )


def _live(message) -> LiveGuild:
    return LiveGuild(SimpleNamespace(get_guild=lambda guild_id: message.guild), GUILD, lambda: False)


async def _handle(message, codex, access=None) -> None:
    await handle_message(
        message,
        live=_live(message),
        bot_user_id=BOT,
        cleanups=AI_CLEANUPS,
        codex=codex,
        access=access or _Access(),
    )


class HandleMessageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        TIPPED.clear()

    async def test_unmentioned_message_is_ignored(self) -> None:
        message = _message("隨便聊聊", mention=False)
        codex = _Codex()
        await _handle(message, codex)
        message.reply.assert_not_awaited()
        codex.try_start_request.assert_not_called()

    async def test_plain_message_reads_no_settings(self) -> None:
        await _handle(_message("隨便聊聊", mention=False), _Codex(), NonCallableMock(spec=[]))

    async def test_disabled_ai_ignores_mention(self) -> None:
        message = _message()
        codex = _Codex()
        access = _Access()
        access.enabled = False
        await _handle(message, codex, access)
        message.channel.send.assert_not_awaited()
        codex.try_start_request.assert_not_called()

    async def test_outside_allowlist_names_up_to_five_ai_channels(self) -> None:
        message = _message()
        codex = _Codex()
        access = _Access(denial="channel")
        access.channel_ids = frozenset({107, 101, 103, 102, 106, 104, 105})
        await _handle(message, codex, access)
        message.channel.send.assert_awaited_once()
        sent = message.channel.send.await_args.kwargs
        self.assertIn("這個頻道不能找霍蘿聊天，請到 <#101>、<#102>、<#103>、<#104>、<#105> 再 @霍蘿。", _text(sent["view"]))
        self.assertFalse(sent["reference"].fail_if_not_exists)
        codex.try_start_request.assert_not_called()

    async def test_missing_role_and_no_ai_channel_get_their_own_notice(self) -> None:
        for denial, channel_ids, expected in (
            ("role", {101}, "你目前沒有使用霍蘿 AI 的身分組，需要的話請找管理員。"),
            ("channel", set(), "霍蘿 目前沒有在這個頻道或你的身分組開放。"),
        ):
            with self.subTest(denial=denial):
                message = _message()
                access = _Access(denial=denial)
                access.channel_ids = frozenset(channel_ids)
                await _handle(message, _Codex(), access)
                text = _text(message.channel.send.await_args.kwargs["view"])
                self.assertIn(expected, text)
                self.assertNotIn("<#", text)

    async def test_reply_ping_outside_allowlist_stays_silent(self) -> None:
        reference = SimpleNamespace(message_id=1, channel_id=CHANNEL, resolved=None)
        message = _message("謝謝", reference=reference)
        codex = _Codex()
        await _handle(message, codex, _Access(denial="channel"))
        message.channel.send.assert_not_awaited()
        codex.try_start_request.assert_not_called()

    async def test_suspended_allowlist_gets_busy_notice(self) -> None:
        message = _message()
        await _handle(message, _Codex(), _Access(denial="suspended"))
        sent = message.channel.send.await_args.kwargs
        self.assertIn(codex_error_text("busy", "霍蘿"), _text(sent["view"]))

    async def test_cooldown_notice(self) -> None:
        message = _message()
        codex = _Codex(allow=False)
        await _handle(message, codex)
        self.assertIn("你發問得太快了", _text(message.channel.send.await_args.kwargs["view"]))
        codex.chat.assert_not_awaited()

    async def test_success_sends_answer_as_new_reply_and_deletes_progress_card(self) -> None:
        message = _message()
        codex = _Codex()
        await _handle(message, codex)
        message.reply.assert_awaited_once()
        self.assertEqual(codex.chat.await_args.args[1:3], ("你好", ()))
        sent = message.channel.send.await_args.kwargs
        self.assertIn("最終答案", _text(sent["view"]))
        self.assertFalse(sent["reference"].fail_if_not_exists)
        message.progress.edit.assert_not_awaited()
        message.progress.delete.assert_awaited_once()

    async def test_answer_notifies_only_the_asker(self) -> None:
        for display in (True, False):
            with self.subTest(display=display):
                message = _message()
                access = _Access()
                access.text_display = display
                await _handle(message, _Codex(), access)
                sent = message.channel.send.await_args
                self.assertEqual(sent.args[:1], (None,) if display else ("最終答案",))
                self.assertEqual(discord.AllowedMentions.none().merge(sent.kwargs["allowed_mentions"]).to_dict(),
                                 {"parse": [], "replied_user": True})

    async def test_restarted_conversation_is_noted_before_answer(self) -> None:
        message = _message()
        codex = _Codex()
        codex.chat.return_value = CodexChatReply("最終答案", restarted=True)
        await _handle(message, codex)
        self.assertIn(
            "-# ℹ️ 已開始新的對話（先前的無法接續）\n最終答案",
            _text(message.channel.send.await_args.kwargs["view"]),
        )

    async def test_all_notes_share_one_line_and_one_icon(self) -> None:
        message = _message()
        codex = _Codex()
        codex.chat.return_value = CodexChatReply("最終答案", restarted=True)
        await handle_message(message, live=_live(message), bot_user_id=BOT, cleanups=AI_CLEANUPS, codex=codex, access=_Access(), bot=SimpleNamespace())
        self.assertEqual(
            _text(message.channel.send.await_args.kwargs["view"]),
            "-# ℹ️ 已開始新的對話（先前的無法接續） · 第一次來？跟我說「說明」看看我能做什麼\n最終答案",
        )

    async def test_help_words_reply_the_help_card_without_ai(self) -> None:
        bot = SimpleNamespace(games=SimpleNamespace(channel_id=99))
        for words in ("說明", "你會什麼？", "你會什麼?"):
            message = _message(f"<@{BOT}>  {words} ")
            codex = _Codex()
            await handle_message(message, live=_live(message), bot_user_id=BOT, cleanups=AI_CLEANUPS, codex=codex, access=_Access(), bot=bot)
            self.assertIn("<#99>", _text(message.channel.send.await_args.kwargs["view"]))
            codex.try_start_request.assert_not_called()
            codex.chat.assert_not_awaited()

    async def test_newcomer_tip_only_on_the_first_answer(self) -> None:
        for tipped in (True, False):
            message = _message(f"<@{BOT}> 說明一下規則")
            codex = _Codex()
            await handle_message(message, live=_live(message), bot_user_id=BOT, cleanups=AI_CLEANUPS, codex=codex, access=_Access(), bot=SimpleNamespace())
            codex.chat.assert_awaited_once()
            text = _text(message.channel.send.await_args.kwargs["view"])
            self.assertEqual(text, "-# ℹ️ 第一次來？跟我說「說明」看看我能做什麼\n最終答案" if tipped else "最終答案")

    async def test_typing_failure_still_sends_answer(self) -> None:
        message = _message()
        response = MagicMock(status=500, reason="down")
        message.channel.typing = MagicMock(side_effect=discord.HTTPException(response, "down"))
        await _handle(message, _Codex())
        self.assertIn("最終答案", _text(message.channel.send.await_args.kwargs["view"]))

    async def test_progress_card_timeout_still_sends_answer(self) -> None:
        message = _message()
        message.reply.side_effect = TimeoutError
        await _handle(message, _Codex())
        self.assertIn("最終答案", _text(message.channel.send.await_args.kwargs["view"]))
        message.progress.edit.assert_not_awaited()

    async def test_busy_service_turns_progress_card_into_error(self) -> None:
        message = _message()
        codex = _Codex(admission_error=CodexError("busy"))
        await _handle(message, codex)
        self.assertIn(
            codex_error_text("busy", "霍蘿"), _text(message.progress.edit.await_args.kwargs["view"])
        )
        message.progress.delete.assert_not_awaited()
        codex.chat.assert_not_awaited()

    async def test_failed_error_card_edit_still_deletes_card(self) -> None:
        message = _message()
        message.progress.edit.side_effect = discord.HTTPException(MagicMock(status=500), "down")
        with self.assertLogs(level="ERROR") as logs:
            await _handle(message, _Codex(admission_error=CodexError("busy")))
        self.assertIn("Failed to send Discord AI status reply.", "\n".join(logs.output))
        message.progress.delete.assert_awaited_once()

    async def test_busy_without_progress_card_replies_in_channel(self) -> None:
        message = _message()
        message.reply.side_effect = TimeoutError
        await _handle(message, _Codex(admission_error=CodexError("busy")))
        sent = message.channel.send.await_args.kwargs
        self.assertIn(codex_error_text("busy", "霍蘿"), _text(sent["view"]))
        self.assertFalse(sent["reference"].fail_if_not_exists)
        message.progress.edit.assert_not_awaited()

    async def test_revoked_mid_request_deletes_progress_card(self) -> None:
        message = _message()
        codex = _Codex()
        access = _Access()

        async def revoke(*args, **kwargs):
            access.current = False
            return CodexChatReply("不該送出")

        codex.chat.side_effect = revoke
        await _handle(message, codex, access)
        message.progress.edit.assert_not_awaited()
        message.progress.delete.assert_awaited_once()
        message.channel.send.assert_not_awaited()

    async def test_overlong_question_notice(self) -> None:
        message = _message(f"<@{BOT}> " + "字" * (MAX_PROMPT_CHARACTERS + 1))
        codex = _Codex()
        await _handle(message, codex)
        self.assertIn(
            f"問題最多 {MAX_PROMPT_CHARACTERS:,} 個字元",
            _text(message.progress.edit.await_args.kwargs["view"]),
        )
        codex.chat.assert_not_awaited()

    async def test_cancel_deletes_progress_card_without_raising(self) -> None:
        message = _message()
        codex = _Codex()
        started = asyncio.Event()

        async def hang(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        codex.chat.side_effect = hang
        task = asyncio.create_task(_handle(message, codex))
        await started.wait()
        task.cancel()
        self.assertIsNone(await task)
        message.progress.delete.assert_awaited_once()
        message.progress.edit.assert_not_awaited()

    async def test_chat_may_outlive_work_deadline_before_job_deadline(self) -> None:
        message = _message()
        codex = _Codex(work_seconds=0.05)

        async def slow(*args, **kwargs):
            await asyncio.sleep(0.2)
            return CodexChatReply("慢慢來的答案")

        codex.chat.side_effect = slow
        await _handle(message, codex)
        self.assertIn("慢慢來的答案", _text(message.channel.send.await_args.kwargs["view"]))

    async def test_reply_to_bot_without_ping_carries_its_text_and_media(self) -> None:
        referenced = SimpleNamespace(
            author=SimpleNamespace(id=BOT),
            content="被回覆文字",
            attachments=[],
            embeds=[],
            stickers=[SimpleNamespace(id=7, url=STICKER_URL, format=SimpleNamespace(name="png"))],
            components=[],
        )
        message = _message(
            "這是什麼",
            mention=False,
            reference=SimpleNamespace(channel_id=CHANNEL, resolved=referenced, message_id=99),
        )
        codex = _Codex()

        async def read_media(groups, *, deadline):
            return [[("靜態圖", (f"img:{url}",)) for _kind, url in group] for group in groups], 0, 0, 0

        with patch("src.ai.discord._read_media", side_effect=read_media):
            await _handle(message, codex)
        _key, prompt, images = codex.chat.await_args.args
        self.assertEqual(images, (f"img:{STICKER_URL}",))
        self.assertIn("這是什麼", prompt)
        self.assertIn("【被回覆的訊息】\n被回覆文字", prompt)
        self.assertIn("最終答案", _text(message.channel.send.await_args.kwargs["view"]))

    async def test_unexpected_error_turns_progress_card_into_error(self) -> None:
        message = _message()
        codex = _Codex()
        codex.chat.side_effect = sqlite3.OperationalError("locked")
        with self.assertLogs(level="ERROR") as logs:
            await handle_message(message, live=_live(message), bot_user_id=BOT, cleanups=AI_CLEANUPS, codex=codex, access=_Access(), bot=SimpleNamespace())
        self.assertIn("failed unexpectedly", "\n".join(logs.output))
        self.assertIn(
            codex_error_text("unavailable", "霍蘿"), _text(message.progress.edit.await_args.kwargs["view"])
        )
        message.progress.delete.assert_not_awaited()
        message.channel.send.assert_not_awaited()

    async def test_unreadable_media_is_noted_or_reported(self) -> None:
        async def read_media(groups, *, deadline):
            return [[]], 0, 1, 0

        codex = _Codex()
        with patch("src.ai.discord._read_media", side_effect=read_media):
            await _handle(_message(), codex)
        self.assertIn("另有 1 個媒體無法讀取", codex.chat.await_args.args[1])
        message = _message(f"<@{BOT}>")
        message.stickers = [SimpleNamespace(id=7, url=STICKER_URL, format=SimpleNamespace(name="png"))]
        codex = _Codex()
        with patch("src.ai.discord._read_media", side_effect=read_media):
            await _handle(message, codex)
        self.assertIn("目前無法讀取這些媒體", _text(message.progress.edit.await_args.kwargs["view"]))
        codex.chat.assert_not_awaited()

    async def test_unsupported_image_is_noted_and_answered(self) -> None:
        message = _message()
        message.attachments = [_attachment(1, "a.svg", "image/svg+xml")]
        codex = _Codex()
        await _handle(message, codex)
        _key, prompt, images = codex.chat.await_args.args
        self.assertEqual(images, ())
        self.assertIn("另有 1 個媒體無法讀取", prompt)
        self.assertIn("最終答案", _text(message.channel.send.await_args.kwargs["view"]))

    async def test_media_deadline_keeps_what_was_read(self) -> None:
        async def download(session, url, total_before):
            await asyncio.sleep(0)
            if "slow" in url:
                await asyncio.Event().wait()
            return PNG

        message = _message()
        message.attachments = [_attachment(1, "a.png", "image/png"), _attachment(2, "slow.png", "image/png")]
        codex = _Codex()
        with (
            patch("src.ai.discord.IMAGE_TIMEOUT_SECONDS", 0.05),
            patch("src.ai.images._download_discord_media", side_effect=download),
        ):
            await _handle(message, codex)
        _key, prompt, images = codex.chat.await_args.args
        self.assertEqual(len(images), 1)
        self.assertIn("另有 1 個媒體逾時未讀取", prompt)
        message.progress.edit.assert_not_awaited()
        self.assertIn("最終答案", _text(message.channel.send.await_args.kwargs["view"]))


class AskTests(unittest.IsolatedAsyncioTestCase):
    async def test_not_current_claims_nothing(self) -> None:
        codex = SimpleNamespace(accepted_request=MagicMock())
        self.assertIsNone(await ask(codex, _Access(), "key", "問題", lambda: False))
        codex.accepted_request.assert_not_called()

    async def test_answer_or_none_on_failure(self) -> None:
        codex = _Codex()
        self.assertEqual(await ask(codex, _Access(), "key", "問題", lambda: True), "最終答案")
        self.assertIsNone(codex.generate_once.await_args.kwargs["effort"])
        self.assertEqual(await ask(codex, _Access(), "key", "問題", lambda: True, effort="low"), "最終答案")
        self.assertEqual(codex.generate_once.await_args.kwargs["effort"], "low")
        codex.generate_once.side_effect = CodexError("unavailable")
        with self.assertLogs(level="ERROR"):
            self.assertIsNone(await ask(codex, _Access(), "key", "問題", lambda: True))

    async def test_cancelled_child_does_not_cancel_background_caller(self) -> None:
        codex = _Codex()
        codex.generate_once.side_effect = asyncio.CancelledError
        self.assertIsNone(await ask(codex, _Access(), "key", "問題", lambda: True))
        self.assertEqual(asyncio.current_task().cancelling(), 0)

    async def test_caller_cancellation_stops_its_child_and_propagates(self) -> None:
        codex = _Codex()
        entered, stopped = asyncio.Event(), asyncio.Event()

        async def generating(*_args, **_kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        codex.generate_once.side_effect = generating
        caller = asyncio.create_task(ask(codex, _Access(), "key", "問題", lambda: True))
        await entered.wait()
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        self.assertTrue(stopped.is_set())


class AskOnceTests(unittest.IsolatedAsyncioTestCase):
    def bot(self, counts=(0, 0)) -> SimpleNamespace:
        codex = _Codex()
        codex.request_counts = MagicMock(return_value=counts)
        codex.accepted_request = MagicMock(wraps=codex.accepted_request)
        return SimpleNamespace(ai_settings=_Access(), codex=codex, guild_id=GUILD)

    async def test_each_question_takes_a_fresh_key(self) -> None:
        bot = self.bot()
        self.assertEqual(await ask_once(bot, CHANNEL, "問題", timeout=5, effort="low", kind="test"), "最終答案")
        with self.assertLogs(level="INFO") as logs:
            self.assertEqual(await ask_once(bot, CHANNEL, "問題", timeout=5, effort="medium", kind="dice"), "最終答案")
        self.assertEqual(bot.codex.generate_once.await_args.kwargs["effort"], "medium")
        self.assertRegex(logs.output[-1], r"Game AI dice effort=medium result=ok ms=\d+$")
        first, second = (call.args[0] for call in bot.codex.accepted_request.call_args_list)
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith(f"guild:{GUILD}:channel:{CHANNEL}:user:"))

    async def test_gives_way_when_off_or_chat_is_waiting(self) -> None:
        for counts, enabled, result in (((0, 1), True, "busy"), ((3, 0), True, "busy"), ((0, 0), False, "off")):
            bot = self.bot(counts)
            bot.ai_settings.enabled = enabled
            with self.subTest(counts=counts, enabled=enabled), self.assertLogs(level="INFO") as logs:
                self.assertIsNone(await ask_once(bot, CHANNEL, "問題", timeout=5, effort="low", kind="test"))
                bot.codex.accepted_request.assert_not_called()
            self.assertIn(f"Game AI test effort=low result={result} ms=", logs.output[-1])

    async def test_gives_way_when_the_quota_is_nearly_used(self) -> None:
        bot = self.bot()
        bot.codex.quota_full.return_value = True
        with self.assertLogs(level="INFO") as logs:
            self.assertIsNone(await ask_once(bot, CHANNEL, "問題", timeout=5, effort="low", kind="test"))
        bot.codex.accepted_request.assert_not_called()
        self.assertIn("Game AI test effort=low result=quota ms=", logs.output[-1])

    async def test_leaves_the_last_slot_for_chat(self) -> None:
        self.assertEqual(await ask_once(self.bot((2, 0)), CHANNEL, "問題", timeout=5, effort="low", kind="test"), "最終答案")

    async def test_slow_answer_is_none(self) -> None:
        bot = self.bot()
        async def slow(*args, **kwargs):
            await asyncio.sleep(5)

        bot.codex.generate_once.side_effect = slow
        with self.assertLogs(level="INFO") as logs:
            self.assertIsNone(await ask_once(bot, CHANNEL, "問題", timeout=0.05, effort="low", kind="remark"))
        self.assertRegex(logs.output[-1], r"Game AI remark effort=low result=timeout ms=\d+$")

    def test_clean_reply_defuses_mentions_and_trims(self) -> None:
        self.assertEqual(clean_reply("  @everyone 好\n", 50), "@\u200beveryone 好")
        self.assertEqual(clean_reply("一二三四五", 3), "一二…")
        self.assertEqual(clean_reply("一二三", 3), "一二三")
        self.assertIsNone(clean_reply(" ", 5))
        self.assertIsNone(clean_reply(None, 5))
        self.assertIn("不是指令", DATA_RULE)

    def test_chat_instructions_carry_the_game_persona(self) -> None:
        self.assertIn(PERSONA, CHAT_INSTRUCTIONS)

    def test_gameplay_summary_lists_only_remaining_features(self) -> None:
        summary = CHAT_INSTRUCTIONS.split("玩法摘要", 1)[1].split("\n\n", 1)[0]
        for text in ("21 點", "拉霸", "不下注", "叫牌、停牌", "臨時語音", "Steam", "說「說明」會出功能卡"):
            self.assertIn(text, summary)
        for text in ("點數來源", "上班", "抽卡", "商店", "樂透", "賽馬", "生日", "語音同樂", "金庫", "任務", "揪團"):
            self.assertNotIn(text, summary)


class AiCogTest(unittest.IsolatedAsyncioTestCase):
    async def test_losing_allowlisted_role_cancels_member_requests(self) -> None:
        bot = MagicMock()
        live = LiveGuild(bot, GUILD, lambda: False)
        bot.ai_settings.enabled = True
        bot.ai_settings.role_ids = frozenset({7})
        bot.codex.cancel = AsyncMock()
        allowed, other = SimpleNamespace(id=7), SimpleNamespace(id=8)
        before = SimpleNamespace(roles=[allowed, other])
        after = SimpleNamespace(roles=[other], id=USER, guild=SimpleNamespace(id=GUILD))
        await AiCog(bot, live, requests=group(REQUEST), cleanups=AI_CLEANUPS).on_member_update(before, after)
        bot.codex.cancel.assert_awaited_once_with(GUILD, USER)
        bot.codex.cancel.reset_mock()
        after.roles = [allowed]
        before.roles = [allowed, other]
        await AiCog(bot, live, requests=group(REQUEST), cleanups=AI_CLEANUPS).on_member_update(before, after)
        bot.codex.cancel.assert_not_called()

    async def test_message_is_a_tracked_request_until_handled(self) -> None:
        bot = MagicMock()
        requests = group(REQUEST)
        cog = AiCog(bot, LiveGuild(bot, GUILD, lambda: False), requests=requests, cleanups=AI_CLEANUPS)
        release = asyncio.Event()
        received = {}

        async def handle(*args: object, **kwargs: object) -> None:
            received.update(kwargs)
            await release.wait()

        with patch("src.ai.discord.handle_message", handle):
            request = asyncio.create_task(cog.on_message(SimpleNamespace(guild=SimpleNamespace(id=GUILD))))
            await asyncio.sleep(0)
            self.assertEqual(requests.pending(), {request})
            self.assertIs(received["cleanups"], AI_CLEANUPS)
            release.set()
            await request
        self.assertEqual(requests.pending(), set())


if __name__ == "__main__":
    unittest.main()

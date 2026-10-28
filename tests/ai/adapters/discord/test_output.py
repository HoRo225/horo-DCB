import asyncio
import contextlib
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from src.ai.adapters.discord.errors import codex_error_text
from tests.support import testing
from src.ai.adapters.discord.output import (
    DISCORD_MESSAGE_LIMIT,
    AiProgress,
    build_ai_native_image_links,
    build_ai_progress_view,
    send_ai_answer,
)
from src.core.adapters.discord.brand import BRAND_COLOUR
from src.core.app.supervisor import PRESENTATION


def card(view: discord.ui.LayoutView) -> str:
    return view.children[0].children[0].content


def progress(cleanups=None, *, deadline: float = 0, can_send=None) -> AiProgress:
    return AiProgress(cleanups=cleanups or testing.group(PRESENTATION), can_send=can_send or AsyncMock(return_value=True),
                      deadline=deadline, name="賢狼")


class ProgressViewTests(unittest.TestCase):
    def test_title_line_and_reasoning_heading(self) -> None:
        view = build_ai_progress_view("generating", "Checking code", 0, name="賢狼")
        self.assertEqual(card(view), "**🧠 賢狼 正在思考** … · 開始於 <t:0:R>\n-# Checking code")
        self.assertEqual(view.children[0].accent_colour, BRAND_COLOUR)
        self.assertEqual(card(build_ai_progress_view("queued", "", None, name="賢狼")), "**🧠 賢狼 正在思考** …")
        self.assertTrue(card(build_ai_progress_view("generating", "a_b", 0, name="賢狼")).endswith("\n-# a\\_b"))

    def test_recovery_stages_have_their_own_titles(self) -> None:
        self.assertIn("賢狼 正在切換備援模型", card(build_ai_progress_view("fallback", "", 0, name="賢狼")))
        self.assertIn("賢狼 連線逾時", card(build_ai_progress_view("reconnecting", "", 0, name="賢狼")))
        self.assertIn("🔍 賢狼 正在搜尋網路", card(build_ai_progress_view("searching", "", 0, name="賢狼")))
        for old in ("等待處理中", "正在整理內容", "正在生成回覆", "正在搜尋資料", "Codex"):
            self.assertNotIn(old, card(build_ai_progress_view("generating", "", 0, name="賢狼")))

    def test_bot_name_is_escaped(self) -> None:
        self.assertIn("**🧠 新\\_名字 正在思考**", card(build_ai_progress_view("queued", "", 0, name="新_名字")))

    def test_longest_progress_summary_fits_the_card(self) -> None:
        self.assertLess(len(card(build_ai_progress_view("generating", "…" + "x" * 1_999, 0, name="賢狼"))), 4_000)


class ErrorTextTests(unittest.TestCase):
    def test_codes_name_the_bot_escaped(self) -> None:
        self.assertEqual(codex_error_text("busy", "*霍蘿*"), "\\*霍蘿\\* 目前忙碌，請稍後再試。")
        self.assertEqual(codex_error_text("nope", "霍蘿"), "霍蘿 暫時無法回覆，請稍後再試。")
        self.assertEqual(codex_error_text("not_allowed_channel", "霍蘿", channels="<#1>"),
                         "這個頻道不能找霍蘿聊天，請到 <#1> 再 @霍蘿。")
        self.assertEqual(codex_error_text("not_allowed_channel", "HoRo", channels="<#1>"),
                         "這個頻道不能找 HoRo 聊天，請到 <#1> 再 @HoRo。")
        self.assertEqual(codex_error_text("not_allowed_role", "HoRo"), "你目前沒有使用 HoRo AI 的身分組，需要的話請找管理員。")
        self.assertNotIn("AI 助手", codex_error_text("auth_required", "霍蘿"))


class ProgressCardTests(unittest.IsolatedAsyncioTestCase):
    async def test_card_timeout_falls_back_without_raising(self) -> None:
        tracker = progress(deadline=asyncio.get_running_loop().time() + 10)
        with self.assertLogs(level="ERROR"):
            await tracker.start(SimpleNamespace(reply=AsyncMock(side_effect=TimeoutError)))
        self.assertIsNone(tracker.message)
        self.assertFalse(await tracker.fail("錯誤"))
        await tracker.finish()

    async def test_card_landing_late_is_removed_on_finish(self) -> None:
        for cancel in (False, True):
            cleanups = testing.group(PRESENTATION)
            posted = SimpleNamespace(delete=AsyncMock())

            async def slow_reply(**kwargs: object) -> object:
                await asyncio.sleep(0.2)
                return posted

            tracker = progress(cleanups, deadline=asyncio.get_running_loop().time() + (10 if cancel else 0.05))
            with contextlib.nullcontext() if cancel else self.assertLogs(level="ERROR"):
                start = asyncio.create_task(tracker.start(SimpleNamespace(reply=slow_reply)))
                await asyncio.sleep(0.1)
                if cancel:
                    start.cancel()
                await asyncio.gather(start, return_exceptions=True)
            self.assertIsNone(tracker.message)
            await tracker.finish()
            await tracker.finish()
            posted.delete.assert_not_awaited()
            self.assertEqual(len(cleanups.pending()), 1)
            await asyncio.gather(*cleanups.pending())
            posted.delete.assert_awaited_once()
            self.assertFalse(cleanups.pending())

    async def test_finish_twice_deletes_the_card_once(self) -> None:
        posted = SimpleNamespace(delete=AsyncMock(), edit=AsyncMock())
        tracker = progress(deadline=asyncio.get_running_loop().time() + 10)
        await tracker.start(SimpleNamespace(reply=AsyncMock(return_value=posted)))
        self.assertIs(tracker.message, posted)
        await tracker.finish()
        await tracker.finish()
        posted.delete.assert_awaited_once()

    async def test_failed_card_stays_as_the_error(self) -> None:
        posted = SimpleNamespace(delete=AsyncMock(), edit=AsyncMock())
        tracker = progress(deadline=asyncio.get_running_loop().time() + 10)
        await tracker.start(SimpleNamespace(reply=AsyncMock(return_value=posted)))
        self.assertTrue(await tracker.fail("出錯了"))
        await tracker.finish()
        self.assertIn("出錯了", testing.text(posted.edit.await_args.kwargs["view"]))
        posted.delete.assert_not_awaited()

    async def test_only_a_new_heading_or_stage_edits_the_card(self) -> None:
        posted = SimpleNamespace(delete=AsyncMock(), edit=AsyncMock())
        tracker = progress(deadline=asyncio.get_running_loop().time() + 10)
        await tracker.start(SimpleNamespace(reply=AsyncMock(return_value=posted)))
        for stage in ("preparing", "generating", "queued"):
            tracker.update(stage, "")
        tracker.update("generating", "**比較做法**\n\n第一段")
        await asyncio.sleep(1.1)
        tracker.update("generating", "**比較做法**\n\n第一段，再多一些")
        await asyncio.sleep(1.1)
        await tracker.finish()
        self.assertEqual([card(call.kwargs["view"]).split("\n-# ")[1] for call in posted.edit.await_args_list],
                         ["比較做法"])


class AnswerTests(unittest.IsolatedAsyncioTestCase):
    async def test_final_answer_is_a_plain_text_reply(self) -> None:
        message = SimpleNamespace(channel=SimpleNamespace(send=AsyncMock()), to_reference=lambda **kwargs: kwargs)
        outcome = await send_ai_answer(message, "最終答案", can_send=AsyncMock(return_value=True))
        self.assertEqual(outcome, "success")
        message.channel.send.assert_awaited_once()
        self.assertEqual(message.channel.send.await_args.args, ("最終答案",))
        self.assertNotIn("view", message.channel.send.await_args.kwargs)
        self.assertIn("reference", message.channel.send.await_args.kwargs)

    async def test_send_failure_is_unavailable(self) -> None:
        error = discord.HTTPException(SimpleNamespace(status=400, reason="failed"), "failed")
        message = SimpleNamespace(channel=SimpleNamespace(send=AsyncMock(side_effect=error)),
                                  to_reference=lambda **kwargs: kwargs)
        with self.assertLogs(level="ERROR"):
            outcome = await send_ai_answer(message, "最終答案", can_send=AsyncMock(return_value=True))
        self.assertEqual(outcome, "unavailable")
        message.channel.send.assert_awaited_once()

    async def test_long_plain_answer_is_split_and_truncated(self) -> None:
        send = AsyncMock()
        message = SimpleNamespace(channel=SimpleNamespace(send=send), to_reference=lambda **kwargs: kwargs)
        await send_ai_answer(message, "word " * 5_000, can_send=AsyncMock(return_value=True))
        chunks = [call.args[0] for call in send.await_args_list]
        self.assertEqual(len(chunks), 8)
        self.assertTrue(all(len(chunk) <= DISCORD_MESSAGE_LIMIT for chunk in chunks))
        self.assertTrue(chunks[-1].endswith("回覆過長，已截斷。"))

    async def test_images_follow_the_text_as_a_link_message(self) -> None:
        send = AsyncMock()
        message = SimpleNamespace(channel=SimpleNamespace(send=send), to_reference=lambda **kwargs: kwargs)
        url = "https://cdn.example.com/a.png"
        await send_ai_answer(message, "word " * 5_000, image_urls=(url,), can_send=AsyncMock(return_value=True))
        chunks = [call.args[0] for call in send.await_args_list]
        self.assertEqual(len(chunks), 8)
        self.assertTrue(chunks[-2].endswith("回覆過長，已截斷。"))
        self.assertTrue(chunks[-1].endswith(url))
        self.assertTrue(all("view" not in call.kwargs for call in send.await_args_list))

    async def test_image_only_answer_replies_with_the_links(self) -> None:
        message = SimpleNamespace(channel=SimpleNamespace(send=AsyncMock()), to_reference=lambda **kwargs: kwargs)
        url = "https://cdn.example.com/a.png"
        self.assertEqual(await send_ai_answer(message, "", image_urls=(url,), can_send=AsyncMock(return_value=True)),
                         "success")
        message.channel.send.assert_awaited_once()
        self.assertTrue(message.channel.send.await_args.args[0].endswith(url))
        self.assertIn("reference", message.channel.send.await_args.kwargs)


class ImageLinksTests(unittest.TestCase):
    def test_empty(self) -> None:
        self.assertEqual(build_ai_native_image_links(()), "")

    def test_long_links_are_omitted_with_note(self) -> None:
        urls = tuple(f"https://cdn.example.com/{i}/" + "a" * 300 for i in range(10))
        content = build_ai_native_image_links(urls)
        self.assertLessEqual(len(content), DISCORD_MESSAGE_LIMIT)
        self.assertRegex(content, r"另有 \d+ 個圖片連結")


if __name__ == "__main__":
    unittest.main()

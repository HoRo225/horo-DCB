from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
from src.ai.formatting import codex_error_text, split_reasoning_summary
from src.ai.output import AiProgress, build_ai_progress_view, send_ai_answer
from src.core.adapters.discord.brand import BRAND_COLOUR
from src.testing import AI_CLEANUPS


def _progress_content(stage: str, summary: str = "", name: str = "賢狼") -> str:
    progress = AiProgress(cleanups=AI_CLEANUPS, can_send=AsyncMock(), deadline=0, name=name)
    progress.update(stage, summary)
    view = build_ai_progress_view(*progress._latest, 0, name=name)
    return view.children[0].children[0].content


class ProgressTests(unittest.IsolatedAsyncioTestCase):
    def test_native_summary_rendering(self) -> None:
        content = _progress_content("generating", "**Checking code**\n\n正在追蹤呼叫路徑。")
        self.assertTrue(content.startswith("**🧠 賢狼 正在思考** … · 開始於 <t:0:R>"), content)
        self.assertEqual(content.count("**"), 2, content)
        view = build_ai_progress_view("generating", "", 0, name="賢狼")
        self.assertEqual(view.children[0].accent_colour, BRAND_COLOUR)
        self.assertTrue(content.endswith(" · 開始於 <t:0:R>\n-# Checking code"), content)
        self.assertNotIn("正在追蹤呼叫路徑", content)
        self.assertNotIn(">>>", content)
        self.assertEqual(
            split_reasoning_summary("沒有標題的摘要"), (None, "沒有標題的摘要")
        )
        self.assertNotIn("\n", _progress_content("generating", "沒有標題的摘要"))
        self.assertIn("**🧠 賢狼 正在思考**", _progress_content("queued"))
        self.assertNotIn("\n", _progress_content("queued"))
        self.assertTrue(_progress_content("generating", "**a_b**").endswith("\n-# a\\_b"))

    def test_streaming_title_never_shows_raw_markers(self) -> None:
        for partial in ("**檢查程", "**檢查程式碼*"):
            content = _progress_content("generating", partial)
            self.assertTrue(content.endswith("\n-# 檢查程" + partial[5:].rstrip("*")), content)
            self.assertEqual(content.count("**"), 2, content)
        self.assertEqual(split_reasoning_summary("**"), (None, ""))

    def test_empty_placeholder_and_title_only(self) -> None:
        title, details = split_reasoning_summary("**整理答案**\n\n<!-- -->")
        self.assertEqual((title, details), ("整理答案", ""))
        self.assertNotIn("\n", _progress_content("generating", "<!-- -->"))
        self.assertTrue(_progress_content("generating", "**整理答案**\n\n<!-- -->").endswith("\n-# 整理答案"))

    def test_recovery_overrides_and_old_labels_are_gone(self) -> None:
        self.assertIn("賢狼 正在切換備援模型", _progress_content("fallback"))
        self.assertIn("賢狼 連線逾時", _progress_content("reconnecting"))
        searching = _progress_content("searching", "**查資料**")
        self.assertIn("🔍 賢狼 正在搜尋網路", searching)
        self.assertTrue(searching.endswith("\n-# 查資料"), searching)
        for old in ("等待處理中", "正在整理內容", "正在生成回覆", "正在搜尋資料", "Codex"):
            self.assertNotIn(old, _progress_content("generating"))

    def test_bot_name_follows_current_name_and_is_escaped(self) -> None:
        self.assertIn("**🧠 新\\_名字 正在思考**", _progress_content("queued", name="新_名字"))
        self.assertEqual(codex_error_text("busy", "*霍蘿*"), "\\*霍蘿\\* 目前忙碌，請稍後再試。")
        self.assertEqual(codex_error_text("nope", "霍蘿"), "霍蘿 暫時無法回覆，請稍後再試。")
        self.assertNotIn("AI 助手", codex_error_text("auth_required", "霍蘿"))

    def test_longest_progress_summary_fits_the_card(self) -> None:
        self.assertLess(len(_progress_content("generating", "…" + "x" * 1_999)), 4_000)

    async def test_progress_card_timeout_falls_back_without_raising(self) -> None:
        progress = AiProgress(
            cleanups=AI_CLEANUPS,
            can_send=AsyncMock(return_value=True),
            deadline=asyncio.get_running_loop().time() + 10,
            name="賢狼",
        )
        await progress.start(SimpleNamespace(reply=AsyncMock(side_effect=TimeoutError)))
        self.assertIsNone(progress.message)
        await progress.stop()

    async def test_card_landing_late_is_removed_on_finish(self) -> None:
        for cancel in (False, True):
            card = SimpleNamespace(delete=AsyncMock())

            async def slow_reply(**kwargs: object) -> object:
                await asyncio.sleep(0.2)
                return card

            progress = AiProgress(
                cleanups=AI_CLEANUPS,
                can_send=AsyncMock(return_value=True),
                deadline=asyncio.get_running_loop().time() + (10 if cancel else 0.05),
                name="賢狼",
            )
            start = asyncio.create_task(progress.start(SimpleNamespace(reply=slow_reply)))
            await asyncio.sleep(0.1)
            if cancel:
                start.cancel()
            await asyncio.gather(start, return_exceptions=True)
            self.assertIsNone(progress.message)
            await progress.finish()
            await progress.finish()
            card.delete.assert_not_awaited()
            self.assertEqual(len(AI_CLEANUPS.pending()), 1)
            await asyncio.gather(*AI_CLEANUPS.pending())
            card.delete.assert_awaited_once()
            self.assertFalse(AI_CLEANUPS.pending())

    def test_stages_without_heading_do_not_trigger_edits(self) -> None:
        progress = AiProgress(cleanups=AI_CLEANUPS, can_send=AsyncMock(), deadline=0, name="賢狼")
        for stage in ("preparing", "generating", "queued"):
            progress.update(stage, "")
        self.assertFalse(progress._changed.is_set())
        progress.update("searching")
        self.assertTrue(progress._changed.is_set())

    def test_new_body_under_the_same_title_triggers_no_edit(self) -> None:
        progress = AiProgress(cleanups=AI_CLEANUPS, can_send=AsyncMock(), deadline=0, name="賢狼")
        progress.update("generating", "**比較做法**\n\n第一段")
        self.assertTrue(progress._changed.is_set())
        progress._changed.clear()
        progress.update("generating", "**比較做法**\n\n第一段，再多一些")
        self.assertFalse(progress._changed.is_set())
        progress.update("generating", "**整理答案**\n\n第一段")
        self.assertTrue(progress._changed.is_set())

    async def test_finish_twice_deletes_the_card_once(self) -> None:
        card = SimpleNamespace(delete=AsyncMock(), edit=AsyncMock())
        progress = AiProgress(
            cleanups=AI_CLEANUPS,
            can_send=AsyncMock(return_value=True),
            deadline=asyncio.get_running_loop().time() + 10,
            name="賢狼",
        )
        await progress.start(SimpleNamespace(reply=AsyncMock(return_value=card)))
        self.assertIs(progress.message, card)
        await progress.finish()
        self.assertIsNone(progress.message)
        await progress.finish()
        card.delete.assert_awaited_once()

    async def test_final_answer_is_a_new_reply(self) -> None:
        message = SimpleNamespace(
            channel=SimpleNamespace(send=AsyncMock()), to_reference=lambda **kwargs: kwargs
        )

        outcome = await send_ai_answer(
            message,
            "最終答案",
            text_display_enabled=True,
            can_send=AsyncMock(return_value=True),
        )

        self.assertEqual(outcome, "success")
        message.channel.send.assert_awaited_once()

    async def test_plain_text_fallback_requires_a_format_rejection(self) -> None:
        for status in (400, 403, 404, 429, 500, 503):
            with self.subTest(status):
                error = discord.HTTPException(SimpleNamespace(status=status, reason="failed"), "failed")
                send = AsyncMock(side_effect=[error, None])
                message = SimpleNamespace(
                    channel=SimpleNamespace(send=send), to_reference=lambda **kwargs: kwargs
                )
                with self.assertLogs(level="ERROR"):
                    outcome = await send_ai_answer(
                        message, "最終答案", text_display_enabled=True,
                        can_send=AsyncMock(return_value=True),
                    )
                self.assertEqual(outcome, "success" if status == 400 else "unavailable")
                self.assertEqual(send.await_count, 2 if status == 400 else 1)
                if status == 400:
                    self.assertEqual(send.await_args.args, ("最終答案",))

if __name__ == "__main__":
    unittest.main()

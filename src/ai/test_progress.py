from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
from src.ai.formatting import codex_error_text, split_reasoning_summary
from src.ai.output import CARD_CLEANUPS, AiProgress, build_ai_progress_view, send_ai_answer
from src.codex.protocol import truncate_progress_summary
from src.codex.rpc import Notification, ThreadEvents
from src.codex.turns import collect_turn
from src.brand import BRAND_COLOUR


def _progress_content(stage: str, summary: str = "", name: str = "賢狼") -> str:
    progress = AiProgress(can_send=AsyncMock(), deadline=0, name=name)
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

    def test_progress_frame_marks_truncation(self) -> None:
        stage, summary = "generating", truncate_progress_summary("x" * 2_100)
        self.assertEqual(len(summary), 2_000)
        self.assertTrue(summary.startswith("…"))
        self.assertLess(len(_progress_content(stage, summary)), 4_000)

    async def test_progress_card_timeout_falls_back_without_raising(self) -> None:
        progress = AiProgress(
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
            self.assertEqual(len(CARD_CLEANUPS), 1)
            await asyncio.gather(*CARD_CLEANUPS)
            card.delete.assert_awaited_once()
            self.assertFalse(CARD_CLEANUPS)

    def test_stages_without_heading_do_not_trigger_edits(self) -> None:
        progress = AiProgress(can_send=AsyncMock(), deadline=0, name="賢狼")
        for stage in ("preparing", "generating", "queued"):
            progress.update(stage, "")
        self.assertFalse(progress._changed.is_set())
        progress.update("searching")
        self.assertTrue(progress._changed.is_set())

    def test_new_body_under_the_same_title_triggers_no_edit(self) -> None:
        progress = AiProgress(can_send=AsyncMock(), deadline=0, name="賢狼")
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

    async def test_latest_summary_part_wins_and_raw_reasoning_is_hidden(self) -> None:
        common = {"threadId": "thread", "turnId": "turn", "itemId": "reasoning"}
        events = ThreadEvents("thread")
        for method, params in (
            ("item/reasoning/summaryTextDelta", {**common, "summaryIndex": 0, "delta": "**第一步**\n\n舊摘要"}),
            ("item/reasoning/summaryPartAdded", {**common, "summaryIndex": 1}),
            ("item/reasoning/textDelta", {**common, "contentIndex": 0, "delta": "不得顯示的原始推理"}),
            ("item/reasoning/summaryTextDelta", {**common, "summaryIndex": 1, "delta": "**第二步**\n\n最新摘要"}),
            ("turn/completed", {"threadId": "thread", "turn": {"id": "turn", "status": "completed", "error": None}}),
        ):
            events.push(Notification(method, params))
        updates: list[tuple[str, str]] = []

        result = await collect_turn(events, "turn", lambda stage, summary: updates.append((stage, summary)))

        self.assertEqual(result.status, "completed")
        self.assertEqual(updates[-1][1], "**第二步**\n\n最新摘要")
        self.assertTrue(all("原始推理" not in summary for _stage, summary in updates))

    async def test_only_tools_and_server_requests_make_an_isolated_turn_untrusted(self) -> None:
        scope = {"threadId": "thread", "turnId": "turn"}
        cases = (
            (Notification("turn/moderationMetadata", scope), None),
            (Notification("turn/somethingNew", scope), None),
            (Notification("item/started", {**scope, "item": {"type": "reasoning", "id": "r"}}), None),
            (Notification("item/started", {**scope, "item": {"type": "commandExecution", "id": "c"}}), "item commandExecution"),
            (Notification("item/fileChange/requestApproval", {**scope, "itemId": "f"}), "request item/fileChange/requestApproval"),
        )
        for event, untrusted in cases:
            with self.subTest(event.method):
                events = ThreadEvents("thread")
                events.push(event)
                events.push(Notification("turn/completed", {"threadId": "thread", "turn": {"id": "turn", "status": "completed"}}))
                result = await collect_turn(events, "turn", isolated=True)
                self.assertEqual((result.status, result.untrusted), ("completed", untrusted))

    async def test_events_for_other_turns_are_ignored(self) -> None:
        events = ThreadEvents("thread")
        events.push(Notification("item/completed", {"threadId": "thread", "turnId": "old",
                                                    "item": {"type": "agentMessage", "text": "舊回覆", "phase": "final_answer"}}))
        events.push(Notification("turn/completed", {"threadId": "thread", "turn": {"id": "old", "status": "completed"}}))
        events.push(Notification("item/completed", {"threadId": "thread", "turnId": "turn",
                                                    "item": {"type": "agentMessage", "text": "新回覆", "phase": None}}))
        events.push(Notification("turn/completed", {"threadId": "thread", "turn": {"id": "turn", "status": "failed",
                                                                                 "error": {"message": "x"}}}))
        result = await collect_turn(events, "turn")
        self.assertEqual((result.status, result.text, result.error), ("failed", "新回覆", {"message": "x"}))

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

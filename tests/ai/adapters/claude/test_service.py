import asyncio
import unittest
from unittest.mock import patch

from src.ai import ModelChoice, ModelSettings
from src.ai.adapters.claude.client import ClaudeClient
from src.ai.adapters.claude.memory import ConversationMemory
from src.ai.adapters.claude.service import ClaudeService
from src.ai.app.ports import ModelError
from tests.support.claude_server import PASSWORD, FakeClewdr, answer, failure, stream_failure

KEY = "guild:1:channel:2:user:3"
OTHER_USER = "guild:1:channel:2:user:4"
THREAD = "guild:1:thread:9"
OTHER_GUILD = "guild:5:channel:2:user:3"
SETTINGS = ModelSettings(ModelChoice("claude-haiku-5-5"), ModelChoice("claude-sonnet-5-5"))
ALONE = ModelSettings(ModelChoice("claude-haiku-5-5-thinking"), None)
PNG = "data:image/png;base64,iVBORw0KGgo="
INSTRUCTIONS = "測試用的聊天指示。"


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.server = FakeClewdr()
        await self.server.start()
        self.addAsyncCleanup(self.server.close)
        self.now = 0.0
        self.service = self.make_service()

    def make_service(self, url: str | None = None, **memory: object) -> ClaudeService:
        service = ClaudeService(ClaudeClient(url or self.server.url, PASSWORD),
                                ConversationMemory(clock=lambda: self.now, **memory))

        async def close() -> None:
            await service.close(deadline=asyncio.get_running_loop().time() + 1)

        self.addAsyncCleanup(close)
        return service

    async def chat(self, text: str = "你好", *, key: str = KEY, images: tuple[str, ...] = (), models=SETTINGS,
                   progress: list | None = None, parent: int | None = None, seconds: float = 30,
                   service: ClaudeService | None = None) -> str:
        service = service or self.service
        deadline = asyncio.get_running_loop().time() + seconds
        on_progress = None if progress is None else lambda stage, summary: progress.append((stage, summary))
        async with service.accepted_request(key, deadline=deadline, still_current=lambda: True,
                                            user_id=3, parent_channel_id=parent) as job:
            reply = await service.chat(key, text, images, instructions=INSTRUCTIONS, job=job, models=models,
                                       on_progress=on_progress, request_id="req1")
        return reply.text

    async def chat_error(self, **kwargs: object) -> str:
        with self.assertRaises(ModelError) as caught:
            await self.chat(**kwargs)
        return caught.exception.code

    def sent_messages(self, index: int = -1) -> list[dict]:
        return self.server.requests[index]["messages"]

    async def test_request_body_carries_instructions_tools_and_image_input(self) -> None:
        self.server.replies = [answer("看到一張圖。")]
        self.assertEqual(await self.chat("這是什麼？", images=(PNG, "https://example.com/a.png")), "看到一張圖。")
        (body,) = self.server.requests
        self.assertEqual(body, {
            "model": "claude-haiku-5-5",
            "max_tokens": 16_000,
            "system": INSTRUCTIONS,
            "messages": [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}},
                {"type": "image", "source": {"type": "url", "url": "https://example.com/a.png"}},
                {"type": "text", "text": "這是什麼？"},
            ]}],
            "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
            "stream": True,
            "metadata": {"user_id": "req1"},
        })

    async def test_progress_follows_thinking_then_search_then_writing(self) -> None:
        progress: list[tuple[str, str]] = []
        self.server.replies = [answer("根據搜尋，", "今天晴天。", thinking=("**查天氣**", "\n\n要搜尋"), search=True)]
        self.assertEqual(await self.chat("今天天氣？", progress=progress), "根據搜尋，今天晴天。")
        self.assertEqual(progress, [
            ("thinking", ""), ("thinking", "**查天氣**"), ("thinking", "**查天氣**\n\n要搜尋"),
            ("searching", ""), ("generating", ""), ("generating", ""),
        ])

    async def test_omitted_thinking_shows_the_stage_without_a_summary(self) -> None:
        progress: list[tuple[str, str]] = []
        self.server.replies = [answer("好。", thinking=())]
        self.assertEqual(await self.chat(models=ALONE, progress=progress), "好。")
        self.assertEqual(progress, [("thinking", ""), ("generating", "")])
        self.assertEqual(self.server.bodies("model"), ["claude-haiku-5-5-thinking"])

    async def test_unknown_events_and_blocks_are_ignored(self) -> None:
        extras = ({"type": "future_event"}, {"type": "content_block_start", "index": 9, "content_block": {"type": "mystery"}},
                  {"type": "content_block_delta", "index": 9, "delta": {"type": "mystery_delta", "x": 1}})
        self.server.replies = [answer("照常回覆。", extras=extras)]
        self.assertEqual(await self.chat(), "照常回覆。")

    async def test_follow_ups_carry_the_conversation_and_keys_stay_apart(self) -> None:
        self.server.replies = [answer("第一答"), answer("第二答"), answer("別人的答")]
        await self.chat("第一問", images=(PNG,))
        await self.chat("第二問")
        self.assertEqual(self.sent_messages(), [
            {"role": "user", "content": "第一問"},
            {"role": "assistant", "content": "第一答"},
            {"role": "user", "content": [{"type": "text", "text": "第二問"}]},
        ])
        await self.chat("我是別人", key=OTHER_USER)
        self.assertEqual(self.sent_messages(), [{"role": "user", "content": [{"type": "text", "text": "我是別人"}]}])

    async def test_long_history_is_trimmed_from_the_oldest_turn(self) -> None:
        service = self.make_service(max_chars=10)
        self.server.replies = [answer("一二三"), answer("四五六"), answer("好")]
        await self.chat("問一問", service=service)
        await self.chat("問二問", service=service)
        await self.chat("問三", service=service)
        self.assertEqual(self.sent_messages()[:-1], [
            {"role": "user", "content": "問二問"}, {"role": "assistant", "content": "四五六"},
        ])

    async def test_a_day_of_silence_forgets_the_conversation(self) -> None:
        await self.chat("記得我")
        self.now = 86_400
        await self.chat("還記得嗎")
        self.assertEqual(len(self.sent_messages()), 1)

    async def test_overloaded_primary_falls_back_once(self) -> None:
        for reply in (failure(529, "overloaded_error"), failure(500, "api_error"), failure(429, "rate_limit_error"),
                      stream_failure("overloaded_error", "寫到一半")):
            with self.subTest(reply.status):
                self.server.requests.clear()
                self.server.replies = [reply, answer("備援答")]
                progress: list[tuple[str, str]] = []
                self.assertEqual(await self.chat(progress=progress), "備援答")
                self.assertEqual(self.server.bodies("model"), ["claude-haiku-5-5", "claude-sonnet-5-5"])
                self.assertIn(("fallback", ""), progress)

    async def test_fallback_failure_is_reported_as_upstream(self) -> None:
        self.server.replies = [failure(529, "overloaded_error"), failure(503, "api_error")]
        self.assertEqual(await self.chat_error(), "upstream")
        self.assertEqual(len(self.server.requests), 2)

    async def test_client_errors_do_not_fall_back(self) -> None:
        for reply, code in ((failure(400, "invalid_request_error"), "invalid_request"),
                            (failure(404, "not_found_error"), "invalid_request"),
                            (failure(401, "authentication_error"), "auth"),
                            (failure(403, "permission_error"), "auth")):
            with self.subTest(reply.status):
                self.server.requests.clear()
                self.server.replies = [reply]
                self.assertEqual(await self.chat_error(), code)
                self.assertEqual(self.server.bodies("model"), ["claude-haiku-5-5"])

    async def test_failed_turns_are_not_remembered(self) -> None:
        self.server.replies = [failure(400, "invalid_request_error", "bad image"), answer("好")]
        await self.chat_error(text="壞掉的問題")
        await self.chat("新問題")
        self.assertEqual(len(self.sent_messages()), 1)

    async def test_stop_reasons(self) -> None:
        for reply, expected in ((answer("被截斷的長", stop_reason="max_tokens"), "被截斷的長"),
                                (answer("先給你這些", stop_reason="pause_turn"), "先給你這些"),
                                (answer(stop_reason="pause_turn"), "upstream"),
                                (answer("我不能", stop_reason="refusal"), "refusal"),
                                (answer(stop_reason="end_turn"), "unavailable")):
            with self.subTest(expected):
                self.server.replies = [reply]
                try:
                    outcome = await self.chat(models=ALONE)
                except ModelError as exc:
                    outcome = exc.code
                self.assertEqual(outcome, expected)

    async def test_unreachable_clewdr_is_unavailable(self) -> None:
        url = self.server.url
        await self.server.close()
        service = self.make_service(url)
        self.assertEqual(await self.chat_error(service=service, models=ALONE), "unavailable")
        status = await service.get_runtime_status()
        self.assertEqual((status.reason, status.version, status.last_error), ("unavailable", None, "unavailable"))

    async def test_runtime_status_names_the_version_and_last_error(self) -> None:
        status = await self.service.get_runtime_status()
        self.assertEqual((status.reason, status.version, status.last_error), ("ready", "v0.13.6", None))
        self.server.replies = [failure(401, "authentication_error")]
        await self.chat_error()
        self.assertEqual((await self.service.get_runtime_status()).last_error, "auth")
        await self.chat()
        self.assertIsNone((await self.service.get_runtime_status()).last_error)

    async def test_start_polls_until_clewdr_answers(self) -> None:
        self.server.version_failures = 2
        with patch("src.ai.adapters.claude.service.POLL_DELAYS", (0.01,)), self.assertLogs(level="INFO") as logs:
            await self.service.start()
            async with asyncio.timeout(5):
                while not any("clewdr reachable: v0.13.6" in line for line in logs.output):
                    await asyncio.sleep(0.01)
        self.assertEqual(self.server.version_calls, 3)
        await asyncio.sleep(0.05)
        self.assertEqual(self.server.version_calls, 3)

    async def test_too_slow_answer_times_out(self) -> None:
        self.server.replies = [answer("想很久", hang=True)]
        self.assertEqual(await self.chat_error(seconds=10.3), "timeout")

    async def test_cancel_stops_the_stream(self) -> None:
        self.server.replies = [answer("想很久", hang=True)]
        task = asyncio.create_task(self.chat())
        async with asyncio.timeout(5):
            while not self.server.requests:
                await asyncio.sleep(0.01)
        await self.service.cancel(1)
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(self.server.disconnected.wait(), 5)

    async def remember(self, *keys: str) -> None:
        for key in keys:
            await self.chat(key=key, parent=2 if key == THREAD else None)

    async def forgotten(self, key: str) -> bool:
        await self.chat("還在嗎", key=key, parent=2 if key == THREAD else None)
        return len(self.sent_messages()) == 1

    async def test_forget_scope_by_thread_channel_user_and_guild(self) -> None:
        await self.remember(KEY, OTHER_USER, OTHER_GUILD)
        await self.remember(THREAD)
        with self.assertLogs(level="INFO") as logs:
            await self.service.forget_scope(1, 9)
        self.assertTrue(any("forgot 1 conversations" in line for line in logs.output))
        self.assertTrue(await self.forgotten(THREAD))
        self.assertFalse(await self.forgotten(KEY))

        await self.remember(THREAD)
        await self.service.forget_scope(1, user_id=3)
        self.assertTrue(await self.forgotten(KEY))
        self.assertFalse(await self.forgotten(OTHER_USER))
        self.assertFalse(await self.forgotten(THREAD))
        self.assertFalse(await self.forgotten(OTHER_GUILD))

        await self.service.forget_scope(1, 2, include_children=True)
        self.assertTrue(await self.forgotten(OTHER_USER))
        self.assertTrue(await self.forgotten(THREAD))
        self.assertFalse(await self.forgotten(OTHER_GUILD))

        await self.service.forget_scope(5)
        self.assertTrue(await self.forgotten(OTHER_GUILD))

    async def test_revoke_stops_and_forgets_only_what_the_predicate_names(self) -> None:
        await self.remember(KEY, OTHER_USER)
        self.server.replies = [answer("想很久", hang=True)]
        task = asyncio.create_task(self.chat("等等", key=KEY))
        async with asyncio.timeout(5):
            while len(self.server.requests) < 3:
                await asyncio.sleep(0.01)
        await self.service.revoke(1, lambda scoped: scoped.user_id == 3)
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(await self.forgotten(KEY))
        self.assertFalse(await self.forgotten(OTHER_USER))

    async def test_closed_service_takes_no_new_work(self) -> None:
        self.service.stop_new_work()
        self.assertEqual(await self.chat_error(), "unavailable")
        self.assertEqual((await self.service.get_runtime_status()).reason, "unavailable")

    async def test_cooldown_is_per_user(self) -> None:
        self.assertTrue(self.service.try_start_request(3))
        self.assertFalse(self.service.try_start_request(3))
        self.assertTrue(self.service.try_start_request(4))


if __name__ == "__main__":
    unittest.main()

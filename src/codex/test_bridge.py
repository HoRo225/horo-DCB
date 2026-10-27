from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

from src.ai.client import QUOTA_SECONDS, CodexBridgeClient
from src.codex.bridge import create_app
from src.codex.model_settings import ModelChoice, ModelSettings
from src.codex.protocol import (
    CodexBridgeError,
    CodexChatReply,
    CodexRuntimeStatus,
    parse_runtime_status,
)
from src.codex.rate_limits import CodexRateLimits, CodexRateWindow

TOKEN = "a" * 64
AUTH = {"Authorization": f"Bearer {TOKEN}"}
STATUS = CodexRuntimeStatus(
    available=True,
    authenticated=True,
    plan=None,
    sdk_version="1.0",
    runtime_version=None,
    thread_count=2,
    ready=True,
    reason="ready",
)


async def _chat(key, text, images, *, models, on_progress, budget_ms, parent_channel_id, request_id):
    on_progress({"stage": "generating", "summary": "想"})
    return CodexChatReply("好")


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.service = SimpleNamespace(
            live=True, status=AsyncMock(return_value=STATUS), chat=AsyncMock(side_effect=_chat),
            models=AsyncMock(return_value={"models": [{"model": "m", "display_name": "M", "default_effort": "medium",
                               "supported_efforts": ["low", "medium"], "input_modalities": ["text"]}], "fetched_at": 1}),
        )
        self.client = TestClient(TestServer(create_app(TOKEN, self.service)))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def test_token_required_except_livez(self) -> None:
        response = await self.client.get("/v1/status")
        self.assertEqual(response.status, 401)
        self.assertEqual(await response.json(), {"error": "unauthorized"})
        response = await self.client.get("/v1/status", headers=AUTH)
        self.assertEqual(parse_runtime_status(await response.json()), STATUS)
        response = await self.client.get("/livez")
        self.assertEqual(response.status, 200)

    async def test_chat_streams_progress_then_reply(self) -> None:
        payload = {
            "conversation_key": "guild:1:channel:2:user:3",
            "text": "問題",
            "images": [],
            "models": {"primary": {"model": "m", "effort": None}, "fallback": None},
        }
        headers = AUTH | {"X-Request-Id": "0123ABCD"}
        response = await self.client.post("/v1/chat", json=payload, headers=headers)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, "application/x-ndjson")
        frames = [json.loads(line) for line in (await response.text()).splitlines()]
        self.assertEqual(frames[0]["type"], "progress")
        self.assertEqual(
            frames[-1], {"type": "completed", "reply": "好", "image_urls": [], "restarted": False}
        )
        self.assertEqual(self.service.chat.call_args.kwargs["request_id"], "-")

    async def test_chat_rejects_invalid_payload(self) -> None:
        for body in ({"conversation_key": "guild:1:channel:2:user:3"}, "not json"):
            kwargs = {"json": body} if isinstance(body, dict) else {"data": body}
            response = await self.client.post("/v1/chat", headers=AUTH, **kwargs)
            self.assertEqual(response.status, 400)
            self.assertEqual(await response.json(), {"error": "invalid_request"})

    async def test_status_failure_is_logged_with_traceback(self) -> None:
        self.service.status.side_effect = RuntimeError("boom")
        with self.assertLogs(level="ERROR") as logs:
            response = await self.client.get("/v1/status", headers=AUTH)
        self.assertEqual(response.status, 503)
        self.assertEqual(await response.json(), {"error": "unavailable"})
        self.assertIn("RuntimeError: boom", logs.output[0])

    async def test_client_with_wrong_token_gets_unauthorized(self) -> None:
        client = CodexBridgeClient(str(self.client.make_url("")), "b" * 64)
        self.addAsyncCleanup(client.close)
        with self.assertLogs(level="ERROR"), self.assertRaises(CodexBridgeError) as caught:
            await client.get_runtime_status()
        self.assertEqual(caught.exception.code, "unauthorized")

    async def test_client_chat_carries_request_id(self) -> None:
        client = CodexBridgeClient(str(self.client.make_url("")), TOKEN)
        self.addAsyncCleanup(client.close)
        key = "guild:1:channel:2:user:3"
        deadline = asyncio.get_running_loop().time() + 30
        async with client.accepted_request(key, deadline=deadline, still_current=lambda: True) as job:
            models = ModelSettings(ModelChoice("m"), None)
            reply = await client.chat(key, "問題", (), job=job, models=models, request_id="0123abcd")
        self.assertEqual(reply, CodexChatReply("好"))
        self.assertEqual(self.service.chat.call_args.kwargs["request_id"], "0123abcd")

    async def test_client_summary_drops_fallback(self) -> None:
        self.service.isolated = AsyncMock(return_value=CodexChatReply("好"))
        client = CodexBridgeClient(str(self.client.make_url("")), TOKEN)
        self.addAsyncCleanup(client.close)
        key = "guild:1:channel:2:user:3"
        deadline = asyncio.get_running_loop().time() + 30
        async with client.accepted_request(key, deadline=deadline, still_current=lambda: True) as job:
            models = ModelSettings(ModelChoice("m"), ModelChoice("m", "low"))
            reply = await client.summarize(key, [{"ok": True}], job=job, models=models)
        self.assertEqual(reply, CodexChatReply("好"))
        self.assertEqual(self.service.isolated.call_args.kwargs["models"],
                         ModelSettings(ModelChoice("m", "medium"), None))


class ClientStallTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.client = CodexBridgeClient("http://bridge", TOKEN)
        self.client._post_mode = AsyncMock(
            side_effect=[CodexBridgeError("stalled"), CodexChatReply("好")]
        )
        self.client.get_runtime_status = AsyncMock(return_value=SimpleNamespace(ready=True))
        self.progress: list[tuple[str, str]] = []

    async def _chat(self, still_current) -> CodexChatReply:
        key = "guild:1:channel:2:user:3"
        deadline = asyncio.get_running_loop().time() + 30
        async with self.client.accepted_request(
            key, deadline=deadline, still_current=still_current
        ) as job:
            return await self.client.chat(
                key,
                "問題",
                (),
                job=job,
                models=ModelSettings(ModelChoice("m"), None),
                on_progress=lambda *frame: self.progress.append(frame),
            )

    async def test_stalled_turn_waits_for_ready_then_resends_once(self) -> None:
        self.assertEqual(await self._chat(lambda: True), CodexChatReply("好"))
        self.assertEqual(self.progress, [("reconnecting", "")])
        self.assertEqual(self.client._post_mode.await_count, 2)
        self.client.get_runtime_status.assert_awaited_once()

    async def test_revoked_during_recovery(self) -> None:
        current = True

        async def status() -> SimpleNamespace:
            nonlocal current
            current = False
            return SimpleNamespace(ready=True)

        self.client.get_runtime_status.side_effect = status
        with self.assertRaises(CodexBridgeError) as caught:
            await self._chat(lambda: current)
        self.assertEqual(caught.exception.code, "revoked")
        self.assertEqual(self.client._post_mode.await_count, 1)


class ClientArchiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_reset_and_scope_archive_reject_inconsistent_counts(self) -> None:
        client = CodexBridgeClient("http://bridge", TOKEN)
        client._request = AsyncMock(return_value={
            "detached_count": 1, "archived_count": 1, "archive_unconfirmed_count": 1,
        })
        for method, args in ((client.reset_conversation, ("guild:1:channel:2:user:3",)),
                             (client.archive_scope, (1, 2))):
            with self.subTest(method=method.__name__), self.assertRaises(CodexBridgeError) as caught:
                await method(*args)
            self.assertEqual(caught.exception.code, "unavailable")


class ClientQuotaTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_in_the_background_and_keeps_the_last_value(self) -> None:
        client = CodexBridgeClient("http://bridge", TOKEN)
        window = lambda used: CodexRateLimits(1, (CodexRateWindow("primary", used, 60, None),), None)
        client.get_rate_limits = AsyncMock(return_value=window(85))
        with patch("src.ai.client.time") as fake_time:
            clock = fake_time.monotonic
            clock.return_value = 1000.0
            self.assertFalse(client.quota_full())
            client.get_rate_limits.assert_not_awaited()
            with self.assertLogs(level="INFO") as logs:
                await asyncio.sleep(0)
            self.assertIn("Codex quota reached 80%", logs.output[-1])
            self.assertTrue(client.quota_full())
            clock.return_value += QUOTA_SECONDS - 1
            client.get_rate_limits.return_value = window(10)
            self.assertTrue(client.quota_full())
            await asyncio.sleep(0)
            self.assertEqual(client.get_rate_limits.await_count, 1)
            clock.return_value += 1
            client.get_rate_limits.return_value = CodexRateLimits(error="timeout")
            self.assertTrue(client.quota_full())
            await asyncio.sleep(0)
            self.assertEqual(client.get_rate_limits.await_count, 2)
            self.assertTrue(client.quota_full())
        await client.close()

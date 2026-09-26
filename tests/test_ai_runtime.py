import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from openai_codex.models import Notification
from openai_codex.generated.v2_all import (
    AgentMessageDeltaNotification, AgentMessageThreadItem, ItemCompletedNotification,
    MessagePhase, ThreadItem, Turn, TurnCompletedNotification, TurnError, TurnStatus,
)
from src.ai.protocol import CodexBridgeError
from src.ai.bridge import create_app
from src.ai.runtime import CodexService, PRIMARY_MODEL, CAPACITY_FALLBACK_MODEL
from src.ai.thread_store import ThreadStore


def event(method, **payload):
    return Notification(method=method, payload=SimpleNamespace(**payload))


def terminal(status="completed", error=None):
    return Notification(method="turn/completed", payload=TurnCompletedNotification(
        thread_id="sdk-1", turn=Turn(id="turn-1", items=[], status=TurnStatus(status),
                                     error=TurnError(message=error) if error else None)))


def message(text="answer", phase="final_answer"):
    return Notification(method="item/completed", payload=ItemCompletedNotification(
        turn_id="turn-1", thread_id="sdk-1", completed_at_ms=0, item=ThreadItem(root=AgentMessageThreadItem(
            type="agentMessage", id="item-1", text=text, phase=MessagePhase(phase)))))


def delta(text):
    return Notification(method="item/agentMessage/delta", payload=AgentMessageDeltaNotification(
        turn_id="turn-1", thread_id="sdk-1", item_id="item-1", delta=text))


class Handle:
    id = "turn-1"
    thread_id = "sdk-1"

    def __init__(self, events=None, blocked=False):
        self.events = events if events is not None else [message(), terminal()]
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()
        self.interrupts = 0
        self.started = asyncio.Event()

    async def stream(self):
        self.started.set()
        await self.release.wait()
        for item in self.events:
            yield item

    async def interrupt(self):
        self.interrupts += 1
        self.events = [terminal("interrupted")]
        self.release.set()


class Thread:
    id = "sdk-1"

    def __init__(self, codex):
        self.codex = codex

    async def turn(self, inputs):
        self.codex.turn_started.set()
        if self.codex.turn_wait is not None:
            await self.codex.turn_wait.wait()
        return self.codex.handles.pop(0)


class Codex:
    def __init__(self, handles=None):
        self.handles = handles if handles is not None else [Handle()]
        self.models = []
        self.account_calls = 0
        self.close_calls = 0
        self.archive_calls = []
        self.account_wait = asyncio.Event()
        self.account_wait.set()
        self.archive_wait = asyncio.Event()
        self.archive_wait.set()
        self.turn_wait = None
        self.turn_started = asyncio.Event()
        self.resume_wait = None
        self.resume_started = asyncio.Event()
        self.archive_error = False
        self.metadata = SimpleNamespace(serverInfo=SimpleNamespace(version="0.156.1"))

    async def __aenter__(self):
        return self

    async def account(self):
        self.account_calls += 1
        await self.account_wait.wait()
        return SimpleNamespace(account=SimpleNamespace(root=SimpleNamespace(plan_type=None)))

    async def thread_start(self, **kwargs):
        self.models.append(kwargs["model"])
        return Thread(self)

    async def thread_resume(self, thread_id, **kwargs):
        self.models.append(kwargs["model"])
        self.resume_started.set()
        if self.resume_wait is not None:
            await self.resume_wait.wait()
        return Thread(self)

    async def thread_archive(self, thread_id):
        self.archive_calls.append(thread_id)
        await self.archive_wait.wait()
        if self.archive_error:
            raise RuntimeError("archive failed")

    async def close(self):
        self.close_calls += 1
        self.account_wait.set()
        self.archive_wait.set()
        if self.turn_wait is not None:
            self.turn_wait.set()


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "mapping.json"
        self.path.write_text(json.dumps({"version": 2, "threads": {}}))
        self.codex = Codex()
        self.service = CodexService(self.codex, ThreadStore(self.path))
        self.service._initialized = True

    async def asyncTearDown(self):
        for task in tuple(self.service._owned):
            if not task.done():
                task.cancel()
        if self.service._owned:
            await asyncio.gather(*tuple(self.service._owned), return_exceptions=True)

    async def test_status_is_cached_and_stale(self):
        self.assertEqual((await self.service.status())["reason"], "status_stale")
        await self.service._read_status()
        for _ in range(10):
            self.assertTrue((await self.service.status())["ready"])
        self.assertEqual(self.codex.account_calls, 1)
        self.service._status_read_at -= 61
        self.assertEqual((await self.service.status())["reason"], "status_stale")
        self.service.store.available = False
        self.assertEqual((await self.service.status())["reason"], "state_unavailable")

    async def test_http_probes_do_not_start_sdk_calls(self):
        async with TestClient(TestServer(create_app("a" * 64, self.service))) as client:
            self.assertEqual((await client.get("/livez")).status, 200)
            self.assertEqual((await client.get("/readyz")).status, 503)
            self.assertEqual((await client.get("/healthz")).status, 503)
            response = await client.get("/v1/status", headers={"Authorization": "Bearer " + "a" * 64})
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["protocol_version"], 2)
        self.assertEqual(self.codex.account_calls, 0)

    async def test_account_monitor_has_one_outstanding_rpc(self):
        self.codex.account_wait.clear()
        task = self.service._own(self.service._monitor())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        for _ in range(10):
            await self.service.status()
        self.assertEqual(self.codex.account_calls, 1)
        task.cancel()
        self.codex.account_wait.set()

    async def test_chat_requires_ready_snapshot_without_refreshing_account(self):
        for state, expected in (
            ("initial", "unavailable"), ("auth", "auth_required"),
            ("stale", "unavailable"), ("error", "unavailable"),
        ):
            with self.subTest(state=state):
                if state != "initial":
                    await self.service._read_status()
                if state == "auth":
                    self.service._account_cache["authenticated"] = False
                elif state == "stale":
                    self.service._status_read_at -= 61
                elif state == "error":
                    self.service._status_error = "unavailable"
                account_calls = self.codex.account_calls
                with self.assertRaises(CodexBridgeError) as caught:
                    await self.service.chat("guild:1:thread:3", "hi", ())
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(self.codex.account_calls, account_calls)
                self.assertEqual(self.codex.models, [])
                self.assertFalse(self.codex.turn_started.is_set())
                self.assertFalse(self.service._admission.active_keys)

    async def test_mapping_failure_during_resume_prevents_turn(self):
        await self.service._read_status()
        key = "guild:1:thread:3"
        self.service.store.set(key, "sdk-1", parent_channel_id=2)
        self.codex.resume_wait = asyncio.Event()
        task = asyncio.create_task(self.service.chat(key, "hi", ()))
        try:
            await asyncio.wait_for(self.codex.resume_started.wait(), timeout=1)
            self.service.store.available = False
            self.codex.resume_wait.set()
            with self.assertRaises(CodexBridgeError) as caught:
                await asyncio.wait_for(task, timeout=1)
            self.assertEqual(caught.exception.code, "unavailable")
            self.assertFalse(self.codex.turn_started.is_set())
            self.assertEqual(self.codex.models, [PRIMARY_MODEL])
            self.assertEqual(self.codex.close_calls, 0)
            self.assertFalse(self.service._draining)
            self.assertFalse(self.service._admission.active_keys)
        finally:
            self.codex.resume_wait.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_mapping_failure_during_turn_retains_owner_until_terminal(self):
        await self.service._read_status()
        key = "guild:1:thread:3"
        handle = Handle(blocked=True)
        self.codex.handles = [handle]
        task = asyncio.create_task(self.service.chat(key, "hi", ()))
        try:
            await asyncio.wait_for(handle.started.wait(), timeout=1)
            self.service.store.available = False
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertIn(key, self.service._admission.active_keys)
            handle.release.set()
            with self.assertRaises(CodexBridgeError) as caught:
                await asyncio.wait_for(task, timeout=1)
            self.assertEqual(caught.exception.code, "unavailable")
            self.assertFalse(self.service._admission.active_keys)
            self.assertEqual(self.codex.models, [PRIMARY_MODEL])
        finally:
            handle.release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_legacy_chat_keeps_known_parent_during_unrelated_archive(self):
        await self.service._read_status()
        key = "guild:1:thread:3"
        self.service.store.set(key, "sdk-1", parent_channel_id=5)
        self.service.store.set("guild:1:thread:4", "sdk-legacy")
        handle = Handle(blocked=True)
        self.codex.handles = [handle]
        task = asyncio.create_task(self.service.chat(key, "hi", ()))
        try:
            await asyncio.wait_for(handle.started.wait(), timeout=1)
            self.assertEqual(self.service._admission.jobs[task].parent_channel_id, 5)
            result = await asyncio.wait_for(self.service.archive_scope(1, 2, include_children=True), timeout=1)
            self.assertEqual(result.detached_count, 1)
            self.assertIsNone(self.service.store.get("guild:1:thread:4"))
            self.assertEqual(self.service.store.get(key), "sdk-1")
            self.assertEqual(self.service.store.get_parent(key), 5)
            self.assertEqual(handle.interrupts, 0)
            self.assertFalse(task.done())
            self.assertEqual(self.codex.archive_calls, ["sdk-legacy"])
            handle.release.set()
            self.assertEqual((await asyncio.wait_for(task, timeout=1)).text, "answer")
        finally:
            handle.release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_success_uses_stream_and_persists_parent(self):
        await self.service._read_status()
        reply = await self.service.chat("guild:1:thread:3", "hi", (), parent_channel_id=2)
        self.assertEqual(reply.text, "answer")
        self.assertEqual(self.service.store.get_parent("guild:1:thread:3"), 2)
        self.assertFalse(self.service._admission.active_keys)

    async def test_confirmed_capacity_failure_falls_back_once(self):
        await self.service._read_status()
        self.codex.handles = [Handle([terminal("failed", "server overloaded")]), Handle()]
        reply = await self.service.chat("guild:1:thread:3", "hi", ())
        self.assertEqual(reply.text, "answer")
        self.assertEqual(self.codex.models, [PRIMARY_MODEL, CAPACITY_FALLBACK_MODEL])

    async def test_partial_or_unknown_output_never_retries(self):
        await self.service._read_status()
        for first in (message("commentary", "commentary"),
                      delta("partial"),
                      event("new/event", turn_id="turn-1")):
            self.codex.models.clear()
            self.codex.handles = [Handle([first, terminal("failed", "server overloaded")]), Handle()]
            with self.assertRaises(CodexBridgeError):
                await self.service.chat("guild:1:thread:3", "hi", ())
            self.assertEqual(self.codex.models, [PRIMARY_MODEL])

    async def test_budget_timeout_interrupts_without_retry(self):
        await self.service._read_status()
        handle = Handle(blocked=True)
        self.codex.handles = [handle]
        with self.assertRaises(CodexBridgeError) as caught:
            await self.service.chat("guild:1:thread:3", "hi", (), budget_ms=10)
        self.assertEqual(caught.exception.code, "timeout")
        self.assertEqual(handle.interrupts, 1)
        self.assertEqual(self.codex.models, [PRIMARY_MODEL])

    async def test_interrupted_terminal_never_returns_partial_reply(self):
        await self.service._read_status()
        self.codex.handles = [Handle([message("partial"), terminal("interrupted")])]
        with self.assertRaises(CodexBridgeError):
            await self.service.chat("guild:1:thread:3", "hi", ())
        self.assertEqual(self.codex.models, [PRIMARY_MODEL])

    async def test_bridge_third_request_is_immediately_busy(self):
        await self.service._read_status()
        first, second = Handle(blocked=True), Handle(blocked=True)
        self.codex.handles = [first, second]
        tasks = [asyncio.create_task(self.service.chat(f"guild:1:thread:{key}", "hi", ())) for key in (3, 4)]
        await first.started.wait()
        await second.started.wait()
        with self.assertRaises(CodexBridgeError) as caught:
            await self.service.chat("guild:1:thread:5", "hi", ())
        self.assertEqual(caught.exception.code, "busy")
        self.assertFalse(self.service._admission.waiting)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def test_repeat_cancel_keeps_one_interrupt_and_owner(self):
        await self.service._read_status()
        handle = Handle(blocked=True)
        self.codex.handles = [handle]
        task = asyncio.create_task(self.service.chat("guild:1:thread:3", "hi", ()))
        await handle.started.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(handle.interrupts, 1)
        self.assertFalse(self.service._admission.active_keys)

    async def test_unknown_handle_stops_runtime_without_retry(self):
        await self.service._read_status()
        self.codex.turn_wait = asyncio.Event()
        task = asyncio.create_task(self.service.chat("guild:1:thread:3", "hi", ()))
        await asyncio.wait_for(self.codex.turn_started.wait(), timeout=1)
        with patch("src.ai.runtime.os._exit") as process_exit:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            process_exit.assert_called_once_with(1)
        self.assertEqual(self.codex.models, [PRIMARY_MODEL])
        self.assertEqual(self.codex.close_calls, 1)
        self.assertTrue(self.service._draining)

    async def test_watchdog_closes_old_rpc_before_exit(self):
        self.codex.account_wait.clear()
        rpc = self.service._rpc(self.service._read_status(), watched=True)
        self.service._rpc_tasks[rpc] -= 31
        with patch("src.ai.runtime.os._exit") as process_exit:
            await self.service._watchdog()
            process_exit.assert_called_once_with(1)
        self.assertEqual(self.codex.close_calls, 1)
        self.assertEqual(self.codex.account_calls, 1)

    async def test_archive_detach_survives_caller_cancel(self):
        key = "guild:1:thread:3"
        self.service.store.set(key, "sdk-1", parent_channel_id=2)
        self.codex.archive_wait.clear()
        task = asyncio.create_task(self.service.archive_scope(1, 2, include_children=True))
        async with asyncio.timeout(1):
            while not self.codex.archive_calls:
                await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(self.service.store.get(key))
        self.assertFalse(self.service._archives)
        self.codex.archive_wait.set()
        await asyncio.gather(*tuple(self.service._owned), return_exceptions=True)

    async def test_archive_reports_unconfirmed_and_does_not_restore_mapping(self):
        self.service.store.set("guild:1:thread:3", "sdk-1", parent_channel_id=2)
        self.service.store.set("guild:1:thread:4", "sdk-2", parent_channel_id=5)
        self.codex.archive_error = True
        result = await self.service.archive_scope(1, 2, include_children=True)
        self.assertEqual((result.detached_count, result.archived_count, result.archive_unconfirmed_count), (1, 0, 1))
        self.assertIsNone(self.service.store.get("guild:1:thread:3"))
        self.assertEqual(self.service.store.get("guild:1:thread:4"), "sdk-2")

    async def test_archive_persist_failure_preserves_original_file(self):
        self.service.store.set("guild:1:thread:3", "sdk-1", parent_channel_id=2)
        original = self.path.read_bytes()
        with patch("src.ai.thread_store.write_json_atomic", side_effect=OSError("disk error")):
            with self.assertRaises(CodexBridgeError):
                await self.service.archive_scope(1, 2, include_children=True)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(self.service.store.available)
        self.assertEqual(self.codex.archive_calls, [])


if __name__ == "__main__":
    unittest.main()

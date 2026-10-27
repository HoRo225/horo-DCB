from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from openai_codex import InvalidRequestError
from openai_codex.generated.v2_all import TurnError

from src.codex.model_settings import ModelChoice, ModelSettings
from src.codex.protocol import (
    CodexArchiveResult,
    CodexBridgeError,
    CodexChatReply,
)
from src.codex import runtime
from src.codex.runtime import CodexService
from src.codex.thread_store import ThreadStore
from src.codex.turns import normalize_error
from src.testing import Fatal, Stream

KEY = "guild:1:channel:2:user:3"
SETTINGS = ModelSettings(ModelChoice("primary"), ModelChoice("fallback", "low"))
CAPACITY = SimpleNamespace(codex_error_info="serverOverloaded")
CATALOG = {"models": [{"model": name, "display_name": name, "default_effort": "medium",
                       "supported_efforts": ["low", "medium"], "input_modalities": ["text"]}
                      for name in ("primary", "fallback")], "fetched_at": 1}


def _events(thread_id: str, turn_id: str, error: object) -> list[object]:
    ids = {"thread_id": thread_id, "turn_id": turn_id}
    status = "failed" if error is not None else "completed"
    turn = SimpleNamespace(id=turn_id, status=status, error=error, items=[])
    done = SimpleNamespace(method="turn/completed", payload=SimpleNamespace(turn=turn, **ids))
    if error is not None:
        return [done]
    item = SimpleNamespace(type="agentMessage", text="答案", phase="final_answer")
    return [SimpleNamespace(method="item/completed", payload=SimpleNamespace(item=item, **ids)), done]


class _FakeCodex:
    def __init__(self, errors: list[object]) -> None:
        self.errors = errors
        self.models: list[tuple[str, str]] = []
        self.resumed: list[str] = []
        self.resume_error: Exception | None = None
        self._client = SimpleNamespace(request=AsyncMock(), _sync=SimpleNamespace(_approval_handler=lambda *_: {}, _coerce_notification=lambda *_: SimpleNamespace(payload=None)))

    def _thread(self, thread_id: str) -> SimpleNamespace:
        async def turn(_inputs, *, model, effort, summary, **_options):
            self.models.append((model, getattr(effort, "value", None)))
            turn_id = f"turn-{len(self.models)}"
            events = _events(thread_id, turn_id, self.errors.pop(0))
            return SimpleNamespace(
                thread_id=thread_id,
                id=turn_id,
                stream=lambda: Stream(events),
                interrupt=AsyncMock(),
            )

        return SimpleNamespace(id=thread_id, turn=turn)

    async def thread_start(self, **_options):
        return self._thread("thread-1")

    async def thread_resume(self, thread_id: str, **_options):
        self.resumed.append(thread_id)
        if self.resume_error is not None:
            raise self.resume_error
        return self._thread(thread_id)


class RuntimeChatTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, codex: _FakeCodex) -> CodexService:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        service = CodexService(codex, ThreadStore(Path(directory.name) / "threads.json"))
        service._initialized = True
        service.models = AsyncMock(return_value=CATALOG)
        return service

    async def test_status_reads_account_live(self) -> None:
        codex = _FakeCodex([])
        codex.metadata = SimpleNamespace(serverInfo=SimpleNamespace(version="0.159.0"))
        codex.account = AsyncMock(return_value=SimpleNamespace(account=None))
        service = self._service(codex)
        self.assertEqual((await service.status()).reason, "auth_required")
        codex.account.return_value = SimpleNamespace(account=SimpleNamespace())
        status = await service.status()
        self.assertEqual((status.reason, status.runtime_version), ("ready", "0.159.0"))

    async def test_new_thread_then_resume(self) -> None:
        codex = _FakeCodex([None, None])
        service = self._service(codex)
        reply = await service.chat(KEY, "問題", (), models=SETTINGS)
        self.assertEqual(reply, CodexChatReply("答案"))
        self.assertEqual(service.store.get(KEY), "thread-1")
        self.assertEqual(codex.resumed, [])

        await service.chat(KEY, "再問", (), models=SETTINGS)
        self.assertEqual(codex.resumed, ["thread-1"])
        codex._client.request.assert_not_awaited()

    async def test_missing_thread_starts_new_one(self) -> None:
        codex = _FakeCodex([None])
        codex.resume_error = InvalidRequestError(-32600, "no rollout found for thread id gone")
        service = self._service(codex)
        service.store.set(KEY, "gone")
        reply = await service.chat(KEY, "問題", (), models=SETTINGS)
        self.assertEqual(reply, CodexChatReply("答案", restarted=True))
        self.assertEqual(service.store.get(KEY), "thread-1")

    async def test_chat_recovers_store_after_failed_write(self) -> None:
        service = self._service(_FakeCodex([None]))
        with patch("src.codex.thread_store.os.replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
            service.store.set(KEY, "lost")
        self.assertFalse(service.store.available)
        self.assertEqual(await service.chat(KEY, "問題", (), models=SETTINGS), CodexChatReply("答案"))
        self.assertTrue(service.store.available)
        self.assertEqual(ThreadStore(service.store.path).get(KEY), "thread-1")

    async def test_other_resume_error_keeps_mapping(self) -> None:
        codex = _FakeCodex([])
        codex.resume_error = InvalidRequestError(-32600, "invalid session id: bad")
        service = self._service(codex)
        service.store.set(KEY, "kept")
        with self.assertRaises(CodexBridgeError):
            await service.chat(KEY, "問題", (), models=SETTINGS)
        self.assertEqual(service.store.get(KEY), "kept")

    async def test_fallback_reuses_restarted_thread(self) -> None:
        codex = _FakeCodex([CAPACITY, None])
        codex.resume_error = InvalidRequestError(-32600, "no rollout found for thread id gone")
        service = self._service(codex)
        service.store.set(KEY, "gone")
        original = codex.thread_resume

        async def resume_once(thread_id: str, **options):
            try:
                return await original(thread_id, **options)
            finally:
                codex.resume_error = None

        codex.thread_resume = resume_once
        reply = await service.chat(KEY, "問題", (), models=SETTINGS)
        self.assertTrue(reply.restarted)
        self.assertEqual(codex.resumed, ["gone", "thread-1"])

    async def test_capacity_failure_falls_back(self) -> None:
        codex = _FakeCodex([CAPACITY, None])
        service = self._service(codex)
        progress: list[dict[str, str]] = []
        reply = await service.chat(KEY, "問題", (), models=SETTINGS, on_progress=progress.append)
        self.assertEqual(reply.text, "答案")
        self.assertEqual(codex.models, [("primary", "medium"), ("fallback", "low")])
        self.assertIn({"stage": "fallback", "summary": ""}, progress)

    async def test_each_model_resolves_its_own_default_effort(self) -> None:
        codex = _FakeCodex([CAPACITY, None])
        service = self._service(codex)
        service.models.return_value = {"models": [
            entry | {"default_effort": "low" if entry["model"] == "fallback" else "medium"}
            for entry in CATALOG["models"]], "fetched_at": 1}
        await service.chat(KEY, "問題", (), models=ModelSettings(ModelChoice("primary"), ModelChoice("fallback")))
        self.assertEqual(codex.models, [("primary", "medium"), ("fallback", "low")])

    async def _hold_key(self, service: CodexService, release: asyncio.Event) -> asyncio.Task:
        entered = asyncio.Event()

        async def hold() -> None:
            deadline = asyncio.get_running_loop().time() + 30
            async with service._admission.claim(KEY, deadline=deadline):
                entered.set()
                await release.wait()

        task = asyncio.create_task(hold())
        await entered.wait()
        return task

    async def test_waits_for_key_finishing_cleanup(self) -> None:
        service = self._service(_FakeCodex([None]))
        release = asyncio.Event()
        holder = await self._hold_key(service, release)
        asyncio.get_running_loop().call_later(0.1, release.set)
        reply = await service.chat(KEY, "問題", (), models=SETTINGS)
        self.assertEqual(reply.text, "答案")
        await holder

    async def test_key_never_released_times_out(self) -> None:
        service = self._service(_FakeCodex([]))
        release = asyncio.Event()
        holder = await self._hold_key(service, release)
        with (
            patch("src.codex.runtime.CLEANUP_SECONDS", 0.05),
            self.assertRaises(CodexBridgeError) as caught,
        ):
            await service.chat(KEY, "問題", (), models=SETTINGS)
        self.assertEqual(caught.exception.code, "timeout")
        release.set()
        await holder


class RuntimeInitializeTests(unittest.IsolatedAsyncioTestCase):
    async def _initialize(self, error: Exception) -> CodexService:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        codex = SimpleNamespace(__aenter__=AsyncMock(side_effect=error), close=AsyncMock())
        service = CodexService(codex, ThreadStore(Path(directory.name) / "threads.json"))

        def fatal() -> None:
            raise Fatal

        service._fatal = fatal
        with self.assertLogs(level="ERROR"):
            await service.initialize()
        return service

    async def test_failure_exits(self) -> None:
        with self.assertRaises(Fatal):
            await self._initialize(RuntimeError("boom"))

    async def test_missing_login_stays_up(self) -> None:
        service = await self._initialize(RuntimeError("not logged in"))
        self.assertEqual(service.last_error, "auth_required")
        self.assertFalse(service._draining)
        await service.close()

    async def test_missing_login_retries_until_initialized(self) -> None:
        with patch("src.codex.runtime.AUTH_RETRY_SECONDS", 0.01):
            service = await self._initialize([RuntimeError("not logged in"), None])
            service.codex.account = AsyncMock(return_value=SimpleNamespace(account=SimpleNamespace()))
            service.codex.metadata = SimpleNamespace(serverInfo=None)
            self.assertEqual((await service.status()).reason, "auth_required")
            with self.assertRaises(CodexBridgeError) as caught:
                await service.chat(KEY, "問題", (), models=SETTINGS)
            self.assertEqual(caught.exception.code, "auth_required")
            self.assertEqual((await service.status()).reason, "auth_required")
            async with asyncio.timeout(1):
                while not service._initialized:
                    await asyncio.sleep(0.01)
        self.assertEqual((await service.status()).reason, "ready")
        await service.close()


class _HangingCodex:
    def __init__(self, *, interruptible: bool) -> None:
        self.gate = asyncio.Event()
        self.streaming = asyncio.Event()
        self.interruptible = interruptible
        self.close = AsyncMock()

    async def thread_start(self, **_options):
        return SimpleNamespace(id="thread-1", turn=self._turn)

    async def _turn(self, _inputs, **_kwargs):
        turn = SimpleNamespace(id="turn-1", status="interrupted", error=None, items=[])
        ids = {"thread_id": "thread-1", "turn_id": "turn-1"}
        done = SimpleNamespace(method="turn/completed", payload=SimpleNamespace(turn=turn, **ids))

        async def stream():
            self.streaming.set()
            await self.gate.wait()
            yield done

        async def interrupt() -> None:
            if self.interruptible:
                self.gate.set()

        return SimpleNamespace(thread_id="thread-1", id="turn-1", stream=stream, interrupt=interrupt)


class RuntimeAbortTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, codex: object) -> CodexService:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        service = CodexService(codex, ThreadStore(Path(directory.name) / "threads.json"))
        service._initialized = True
        service.models = AsyncMock(return_value=CATALOG)
        service._fatal = MagicMock(side_effect=Fatal)
        return service

    async def _chat_until_streaming(self, service: CodexService, codex: _HangingCodex) -> asyncio.Task:
        task = asyncio.create_task(service.chat(KEY, "問題", (), models=SETTINGS))
        self.addCleanup(codex.gate.set)
        await codex.streaming.wait()
        return task

    async def test_watchdog_aborts_on_rpc_timeout(self) -> None:
        codex = SimpleNamespace(close=AsyncMock())
        service = self._service(codex)
        hung = service._rpc(asyncio.Event().wait(), watched=True)
        self.addCleanup(hung.cancel)
        with patch("src.codex.runtime.RPC_TIMEOUT_SECONDS", 0), self.assertRaises(Fatal):
            await service._watchdog()
        codex.close.assert_awaited_once()

    async def test_first_event_timeout_stalls_then_aborts_after_grace(self) -> None:
        codex = _HangingCodex(interruptible=False)
        service = self._service(codex)
        self.addCleanup(codex.gate.set)
        with (
            patch("src.codex.runtime.FIRST_EVENT_SECONDS", 0.05),
            patch("src.codex.runtime.STALL_EXIT_GRACE_SECONDS", 0.05),
            self.assertLogs(level="ERROR"),
        ):
            with self.assertRaises(CodexBridgeError) as caught:
                await service.chat(KEY, "問題", (), models=SETTINGS)
            self.assertEqual(caught.exception.code, "stalled")
            self.assertTrue(service._draining)
            service._fatal.assert_not_called()
            async with asyncio.timeout(1):
                while not service._fatal.called:
                    await asyncio.sleep(0.01)

    async def test_cancel_with_successful_interrupt_does_not_abort(self) -> None:
        codex = _HangingCodex(interruptible=True)
        service = self._service(codex)
        task = await self._chat_until_streaming(service, codex)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        service._fatal.assert_not_called()
        self.assertFalse(service._draining)

    async def test_cancel_with_unfinished_collector_aborts(self) -> None:
        codex = _HangingCodex(interruptible=False)
        service = self._service(codex)
        task = await self._chat_until_streaming(service, codex)
        with patch("src.codex.runtime.INTERRUPT_TIMEOUT_SECONDS", 0.05):
            task.cancel()
            with self.assertRaises(Fatal):
                await task
        codex.close.assert_awaited_once()

    def _gated_start(self) -> tuple[_FakeCodex, asyncio.Event, asyncio.Event]:
        codex = _FakeCodex([None])
        codex.close = AsyncMock()
        started, gate = asyncio.Event(), asyncio.Event()
        original = codex.thread_start

        async def thread_start(**options):
            started.set()
            await gate.wait()
            return await original(**options)

        codex.thread_start = thread_start
        self.addCleanup(gate.set)
        return codex, started, gate

    async def test_cancel_during_thread_start_waits_then_drops_thread(self) -> None:
        for mode in ("chat", "intent"):
            with self.subTest(mode):
                codex, started, gate = self._gated_start()
                service = self._service(codex)
                request = (service.chat(KEY, "問題", (), models=SETTINGS) if mode == "chat"
                           else service.isolated(KEY, "看揪團", mode=mode, context={}, models=SETTINGS))
                task = asyncio.create_task(request)
                await started.wait()
                task.cancel()
                asyncio.get_running_loop().call_later(0.05, gate.set)
                with self.assertRaises(asyncio.CancelledError):
                    await task
                service._fatal.assert_not_called()
                self.assertFalse(service._draining)
                self.assertIsNone(service.store.get(KEY))
                self.assertEqual([call.args for call in codex._client.request.await_args_list],
                                 [("thread/unsubscribe", {"threadId": "thread-1"})] if mode == "intent" else [])

    async def test_cancel_during_thread_start_past_cleanup_deadline_aborts(self) -> None:
        codex, started, _gate = self._gated_start()
        service = self._service(codex)
        task = asyncio.create_task(service.chat(KEY, "問題", (), models=SETTINGS))
        await started.wait()
        with patch("src.codex.runtime.INTERRUPT_TIMEOUT_SECONDS", 0.05), self.assertRaises(Fatal):
            task.cancel()
            async with asyncio.timeout(1):
                await task
        codex.close.assert_awaited_once()

    async def test_archive_counts_failed_archive_as_unconfirmed(self) -> None:
        async def archive(thread_id: str) -> None:
            if thread_id == "thread-b":
                raise RuntimeError("boom")

        codex = SimpleNamespace(thread_archive=AsyncMock(side_effect=archive), close=AsyncMock())
        service = self._service(codex)
        service.store.set(KEY, "thread-a")
        service.store.set("guild:1:channel:2:user:4", "thread-b")
        service.store.set("guild:1:channel:9:user:3", "thread-c")
        with self.assertLogs(level="WARNING") as logs:
            result = await service.archive_scope(1, 2)
        self.assertIn("thread=thread-b error=RuntimeError", logs.output[0])
        self.assertEqual(result, CodexArchiveResult(2, 1, 1))
        self.assertIsNone(service.store.get(KEY))
        self.assertEqual(service.store.get("guild:1:channel:9:user:3"), "thread-c")
        self.assertEqual(service.store.pending_archives, ("thread-b",))
        service._fatal.assert_not_called()

    async def test_pending_archive_retry_recovers_store(self) -> None:
        codex = SimpleNamespace(thread_archive=AsyncMock(), close=AsyncMock())
        service = self._service(codex)
        service.store.set(KEY, "old")
        service.store.detach_many([KEY])
        with patch("src.codex.thread_store.os.replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
            service.store.archive_confirmed("old")
        self.assertFalse(service.store.available)
        task = asyncio.create_task(service._retry_pending_archives())
        self.addCleanup(task.cancel)
        async with asyncio.timeout(1):
            while service.store.pending_archives:
                await asyncio.sleep(0.01)
        self.assertTrue(service.store.available)
        codex.thread_archive.assert_awaited_once_with("old")

    def test_archive_budget_fits_bot_request_timeout(self) -> None:
        self.assertLess(runtime.INTERRUPT_TIMEOUT_SECONDS + runtime.ARCHIVE_TIMEOUT_SECONDS
                        + runtime.ARCHIVE_RPC_TIMEOUT_SECONDS, 2 * runtime.CLEANUP_SECONDS + 2)

    async def test_archive_past_soft_deadline_leaves_rest_pending(self) -> None:
        async def slow(_thread_id: str) -> None:
            await asyncio.sleep(0.1)

        codex = SimpleNamespace(thread_archive=AsyncMock(side_effect=slow), close=AsyncMock())
        service = self._service(codex)
        for user, thread_id in ((3, "thread-a"), (4, "thread-b"), (5, "thread-c")):
            service.store.set(f"guild:1:channel:2:user:{user}", thread_id)
        with patch("src.codex.runtime.ARCHIVE_TIMEOUT_SECONDS", 0.05):
            result = await service.archive_scope(1, 2)
        self.assertEqual(result, CodexArchiveResult(3, 1, 2))
        self.assertEqual(service.store.pending_archives, ("thread-b", "thread-c"))
        codex.thread_archive.assert_awaited_once_with("thread-a")
        service._fatal.assert_not_called()
        self.assertFalse(service._draining)

    async def test_archive_of_missing_thread_is_confirmed(self) -> None:
        codex = SimpleNamespace(close=AsyncMock(), thread_archive=AsyncMock(
            side_effect=InvalidRequestError(-32600, "no rollout found for thread id gone")))
        service = self._service(codex)
        service.store.set(KEY, "gone")
        result = await service.archive_scope(1, 2)
        self.assertEqual(result, CodexArchiveResult(1, 1, 0))
        self.assertFalse(service.store.pending_archives)
        service._fatal.assert_not_called()

    async def test_archive_rpc_timeout_aborts(self) -> None:
        async def hang(_thread_id: str) -> None:
            await asyncio.Event().wait()

        codex = SimpleNamespace(thread_archive=AsyncMock(side_effect=hang), close=AsyncMock())
        service = self._service(codex)
        service.store.set(KEY, "thread-a")
        with patch("src.codex.runtime.ARCHIVE_RPC_TIMEOUT_SECONDS", 0.05), self.assertRaises(Fatal):
            await service.archive_scope(1, 2)
        codex.close.assert_awaited_once()

    async def test_reset_survives_caller_disconnect(self) -> None:
        for stage in ("cancel", "archive"):
            with self.subTest(stage):
                entered, release = asyncio.Event(), asyncio.Event()

                async def pause(*_args, **_kwargs) -> None:
                    entered.set()
                    await release.wait()

                codex = SimpleNamespace(thread_archive=AsyncMock(), close=AsyncMock())
                service = self._service(codex)
                service.store.set(KEY, "old")
                if stage == "cancel":
                    service._admission.cancel_where = AsyncMock(side_effect=pause)
                else:
                    codex.thread_archive.side_effect = pause
                caller = asyncio.create_task(service.reset_conversation(KEY))
                await entered.wait()
                try:
                    caller.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await caller
                    service._fatal.assert_not_called()
                    if stage == "cancel":
                        self.assertEqual(service.store.get(KEY), "old")
                        with self.assertRaises(CodexBridgeError):
                            await service.reset_conversation(KEY)
                        with self.assertRaises(CodexBridgeError) as caught:
                            await service.chat(KEY, "問題", (), models=SETTINGS)
                        self.assertEqual(caught.exception.code, "busy")
                finally:
                    release.set()
                    await asyncio.gather(*tuple(service._owned), return_exceptions=True)
                self.assertIsNone(service.store.get(KEY))
                self.assertFalse(service.store.pending_archives)
                self.assertNotIn(KEY, service._resetting)
                codex.thread_archive.assert_awaited_once_with("old")
                service._fatal.assert_not_called()


class NormalizeErrorTests(unittest.TestCase):
    def test_missing_login_401_is_auth_required(self) -> None:
        error = TurnError.model_validate(
            {"message": "401", "codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 401}}}
        )
        self.assertEqual(normalize_error(error).code, "auth_required")
        error = TurnError.model_validate(
            {"message": "502", "codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 502}}}
        )
        self.assertEqual(normalize_error(error).code, "unavailable")

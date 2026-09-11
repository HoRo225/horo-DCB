import asyncio
import json
import logging
import subprocess
import sys
import textwrap
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openai_codex import TransportClosedError

from src.ai.protocol import BridgeRequestError
from src.ai.runtime import CodexService, ThreadStore

from tests.support.ai import settle, stop_tasks, ControlledCodex


class StoreFailureTest(unittest.TestCase):
    def test_unreadable_mapping_is_not_treated_as_an_empty_new_store(self):
        with patch.object(Path, "exists", return_value=False), patch.object(
            Path, "read_text", side_effect=PermissionError("mapping directory denied"),
        ):
            with self.assertRaises(ValueError):
                ThreadStore(Path("/denied/threads.json"))

    def test_failed_replace_preserves_live_and_durable_mapping_for_set_and_pop(self):
        for operation in ("set", "pop"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "threads.json"
                store = ThreadStore(path)
                key = "guild:1:thread:2"
                store.set(key, "old-thread", updated_at=1)
                durable = path.read_bytes()
                with patch("src.state.os.replace", side_effect=OSError("disk full")):
                    with self.assertRaises(OSError):
                        if operation == "set":
                            store.set(key, "new-thread", updated_at=2)
                        else:
                            store.pop_many([key])
                self.assertEqual(store.get(key), "old-thread")
                self.assertEqual(path.read_bytes(), durable)
                self.assertEqual(ThreadStore(path).get(key), "old-thread")


class BridgeLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = ThreadStore(Path(self.directory.name) / "threads.json")
        self.codex = ControlledCodex()
        self.service = CodexService(self.codex, self.store, timeout_seconds=1)
        self.exits = []
        # Injection only replaces process termination, never SDK lifecycle logic.
        self.service.fatal_exit = self.exits.append
        self.service.interrupt_timeout_seconds = 0.02
        self.tasks = []

    async def asyncTearDown(self):
        for gate in self.codex.gates.values():
            gate.set()
        await stop_tasks(self.tasks)
        self.directory.cleanup()

    def chat_task(self, key="guild:1:thread:2"):
        task = asyncio.create_task(self.service.chat(key, "private prompt", ()))
        self.tasks.append(task)
        return task

    async def bounded_chat_error(self):
        try:
            await asyncio.wait_for(self.service.chat("guild:1:thread:2", "wait", ()), 0.3)
        except BridgeRequestError as error:
            return error.code
        except TimeoutError:
            return "outer watchdog expired"
        return "unexpected success"

    async def test_entire_sdk_start_resume_and_turn_are_in_generation_deadline(self):
        for phase in ("start", "resume", "turn"):
            with self.subTest(phase=phase):
                self.codex = ControlledCodex()
                self.codex.gates[phase] = asyncio.Event()
                self.store = ThreadStore(Path(self.directory.name) / f"{phase}.json")
                self.service = CodexService(self.codex, self.store, timeout_seconds=0.02)
                self.exits.clear()
                self.service.fatal_exit = self.exits.append
                self.service.interrupt_timeout_seconds = 0.02
                if phase == "resume":
                    self.store.set("guild:1:thread:2", "existing")
                self.assertEqual(await self.bounded_chat_error(), "timeout")
                self.assertEqual(self.exits, [1])

    async def test_request_cancellation_interrupts_only_its_turn(self):
        self.codex.gates["run"] = asyncio.Event()
        cancelled = self.chat_task()
        await asyncio.wait_for(self.codex.entered["run"].wait(), 0.3)
        other = self.chat_task("guild:1:thread:3")
        await settle()
        cancelled.cancel()
        await asyncio.wait_for(asyncio.gather(cancelled, return_exceptions=True), 0.3)
        self.assertEqual(self.codex.interrupted, ["thread-1"])
        self.assertFalse(other.done())
        self.assertEqual(self.exits, [])
        self.codex.gates["run"].set()
        self.assertEqual(await asyncio.wait_for(other, 0.3), "answer")

    async def test_timeout_with_hung_interrupt_is_bounded_and_fatal(self):
        self.codex.gates["run"] = asyncio.Event()
        self.codex.gates["interrupt"] = asyncio.Event()
        self.service.timeout_seconds = 0.02
        self.assertEqual(await self.bounded_chat_error(), "timeout")
        self.assertEqual(self.exits, [1])
        self.assertEqual(self.codex.runs, ["thread-1"])

    async def test_failed_interrupt_is_fatal_without_retry(self):
        self.codex.gates["run"] = asyncio.Event()
        self.codex.interrupt_error = RuntimeError("private rpc data")
        self.service.timeout_seconds = 0.02
        self.assertEqual(await self.bounded_chat_error(), "timeout")
        self.assertEqual(self.exits, [1])
        self.assertEqual(self.codex.runs, ["thread-1"])

    async def test_drained_cleanup_preserves_interrupt_and_transport_failure_restarts(self):
        cases = (
            ("interrupt", RuntimeError("private interrupt error"), None, [1]),
            ("transport", None, TransportClosedError("private transport error"), [1]),
            ("turn", None, RuntimeError("private turn error"), []),
        )
        for name, interrupt_error, run_error, expected_exits in cases:
            with self.subTest(failure=name):
                self.codex = ControlledCodex()
                self.store = ThreadStore(Path(self.directory.name) / f"drained-{name}.json")
                self.service = CodexService(self.codex, self.store, timeout_seconds=1)
                self.exits.clear()
                self.service.fatal_exit = self.exits.append
                self.service.interrupt_timeout_seconds = 0.2
                self.codex.gates["run"] = asyncio.Event()
                self.codex.gates["interrupt"] = asyncio.Event()
                job = self.chat_task()
                await asyncio.wait_for(self.codex.entered["run"].wait(), 0.3)
                job.cancel()
                await asyncio.wait_for(self.codex.entered["interrupt"].wait(), 0.3)
                self.codex.run_error = run_error
                self.codex.gates["run"].set()
                await settle()
                self.codex.interrupt_error = interrupt_error
                self.codex.gates["interrupt"].set()
                result = (await asyncio.wait_for(
                    asyncio.gather(job, return_exceptions=True), 0.3,
                ))[0]
                self.assertIsInstance(result, asyncio.CancelledError)
                self.assertEqual(self.exits, expected_exits)
                self.assertEqual(self.codex.runs, ["thread-1"])
                self.assertEqual(self.codex.interrupted, ["thread-1"])
                self.assertEqual(len(self.codex.started), 1)
                self.assertFalse(self.service._admission.jobs)

    async def test_auth_and_quota_errors_do_not_request_process_restart(self):
        for message, expected in (("ChatGPT login required", "auth_required"), ("quota exceeded", "usage_limit_or_unavailable")):
            with self.subTest(expected=expected):
                self.codex.run_error = RuntimeError(message)
                self.assertEqual(await self.bounded_chat_error(), expected)
                self.assertEqual(self.exits, [])

    async def test_archive_cancels_inflight_start_before_removing_scope(self):
        self.codex.gates["start"] = asyncio.Event()
        job = self.chat_task()
        await asyncio.wait_for(self.codex.entered["start"].wait(), 0.3)
        await asyncio.wait_for(self.service.archive_scope(1), 0.3)
        self.codex.gates["start"].set()
        result = (await asyncio.wait_for(asyncio.gather(job, return_exceptions=True), 0.3))[0]
        self.assertIsNone(self.store.get("guild:1:thread:2"))
        self.assertNotEqual(result, "answer")

    async def test_archive_cancels_inflight_resume_before_removing_scope(self):
        self.store.set("guild:1:thread:2", "existing")
        self.codex.gates["resume"] = asyncio.Event()
        job = self.chat_task()
        await asyncio.wait_for(self.codex.entered["resume"].wait(), 0.3)
        await asyncio.wait_for(self.service.archive_scope(1), 0.3)
        self.codex.gates["resume"].set()
        result = (await asyncio.wait_for(asyncio.gather(job, return_exceptions=True), 0.3))[0]
        self.assertNotEqual(result, "answer")
        self.assertIsNone(self.store.get("guild:1:thread:2"))

    async def test_archive_rejects_new_matching_work_until_detach_finishes(self):
        self.store.set("guild:1:thread:2", "existing")
        self.codex.gates["archive"] = asyncio.Event()
        archive = asyncio.create_task(self.service.archive_scope(1))
        self.tasks.append(archive)
        await asyncio.wait_for(self.codex.entered["archive"].wait(), 0.3)
        result = await self.bounded_chat_error()
        self.assertEqual(result, "busy")
        self.assertEqual(self.codex.started, [])
        other = await asyncio.wait_for(self.service.chat("guild:2:thread:2", "allowed", ()), 0.3)
        self.assertEqual(other, "answer")

    async def test_status_bounds_blocked_account_rpc(self):
        self.codex.gates["account"] = asyncio.Event()
        try:
            status = await asyncio.wait_for(self.service.status(), 2.3)
        except TimeoutError:
            self.fail("status account RPC exceeded its two-second bound")
        self.assertFalse(status["available"])
        self.assertNotIn("private", json.dumps(status))

    async def test_status_reports_active_and_queued_jobs_and_clears_them(self):
        self.codex.gates["run"] = asyncio.Event()
        first = self.chat_task()
        await asyncio.wait_for(self.codex.entered["run"].wait(), 0.3)
        second = self.chat_task()
        await settle()
        status = await asyncio.wait_for(self.service.status(), 0.3)
        self.assertEqual(status.get("active_requests"), 1)
        self.assertEqual(status.get("queued_requests"), 1)
        self.codex.gates["run"].set()
        await asyncio.wait_for(asyncio.gather(first, second), 0.3)
        status = await asyncio.wait_for(self.service.status(), 0.3)
        self.assertEqual(status.get("active_requests"), 0)
        self.assertEqual(status.get("queued_requests"), 0)

    async def test_status_keeps_only_safe_last_error(self):
        self.codex.run_error = RuntimeError("private-account@example.com raw rpc payload")
        self.assertEqual(await self.bounded_chat_error(), "unavailable")
        status = await asyncio.wait_for(self.service.status(), 0.3)
        self.assertEqual(status.get("last_error"), "unavailable")
        self.assertNotIn("private-account", json.dumps(status))
        self.assertNotIn("raw rpc", json.dumps(status))


    async def test_mapping_write_failure_stops_queued_and_later_chats_until_fresh_store_recovery(self):
        old_key = "guild:1:thread:9"
        self.store.set(old_key, "old-thread", updated_at=1)
        durable = self.store.path.read_bytes()
        self.codex.gates["start"] = asyncio.Event()
        first = self.chat_task()
        await asyncio.wait_for(self.codex.entered["start"].wait(), 0.3)
        queued = self.chat_task()
        await settle()
        with patch("src.state.os.replace", side_effect=OSError("mapping write failed")) as replace:
            self.codex.gates["start"].set()
            results = await asyncio.wait_for(asyncio.gather(first, queued, return_exceptions=True), 0.3)
            later = "unexpected success"
            try:
                await asyncio.wait_for(self.service.chat(old_key, "later", ()), 0.3)
            except BridgeRequestError as error:
                later = error.code
            status = await asyncio.wait_for(self.service.status(), 0.3)
            self.assertTrue(all(isinstance(result, BridgeRequestError) and result.code == "unavailable" for result in results))
            self.assertEqual(len(self.codex.started), 1, "queued work called the SDK again after mapping persistence failed")
            self.assertEqual(self.codex.resumed, [], "later work resumed an SDK thread while storage was unavailable")
            self.assertEqual(replace.call_count, 1, "mapping writes continued after the first failure")
            self.assertEqual(later, "unavailable")
            self.assertFalse(status["available"])
            self.assertEqual(status.get("last_error"), "unavailable")
            self.assertEqual(self.exits, [], "storage failure must not cause a restart loop")
            self.assertEqual(self.store.get(old_key), "old-thread")
            self.assertIsNone(self.store.get("guild:1:thread:2"))
            self.assertEqual(self.store.path.read_bytes(), durable)
        repaired_codex = ControlledCodex()
        repaired = CodexService(repaired_codex, ThreadStore(self.store.path), timeout_seconds=1)
        repaired.fatal_exit = self.exits.append
        reply = await asyncio.wait_for(repaired.chat(old_key, "after repair", ()), 0.3)
        self.assertEqual(reply, "answer")
        self.assertEqual(repaired_codex.started, [])
        self.assertEqual([entry[0] for entry in repaired_codex.resumed], ["old-thread"])
        self.assertTrue((await asyncio.wait_for(repaired.status(), 0.3))["available"])
        self.assertEqual(self.exits, [])

    async def test_transport_closed_recycles_without_auth_or_quota_misclassification(self):
        for target_phase in ("start", "resume", "turn", "run", "account", "archive"):
            with self.subTest(phase=target_phase):
                self.codex = ControlledCodex()
                self.store = ThreadStore(Path(self.directory.name) / f"transport-{target_phase}.json")
                self.service = CodexService(self.codex, self.store, timeout_seconds=1)
                self.exits.clear()
                self.service.fatal_exit = self.exits.append
                original_pause = self.codex.pause
                async def fail_transport(phase):
                    if phase == target_phase:
                        raise TransportClosedError("ChatGPT login required quota private-stderr-secret")
                    await original_pause(phase)
                self.codex.pause = fail_transport
                if target_phase in ("resume", "archive"):
                    self.store.set("guild:1:thread:2", "old-thread")
                with self.assertLogs(level=logging.INFO) as logs:
                    if target_phase == "account":
                        result = await asyncio.wait_for(self.service.status(), 0.3)
                        self.assertFalse(result["available"])
                    elif target_phase == "archive":
                        outcome = "unexpected success"
                        try:
                            await asyncio.wait_for(self.service.archive_scope(1), 0.3)
                        except BridgeRequestError as error:
                            outcome = error.code
                        self.assertEqual(outcome, "unavailable")
                        self.assertIsNone(self.store.get("guild:1:thread:2"))
                    else:
                        self.assertEqual(await self.bounded_chat_error(), "unavailable")
                    self.assertEqual(self.exits, [1])
                self.assertNotIn("private-stderr-secret", "\n".join(logs.output))
                self.assertEqual(self.codex.interrupted, [])

    async def test_blocked_account_rpc_recycles_instead_of_leaking_an_executor_waiter(self):
        self.codex.gates["account"] = asyncio.Event()
        result = await asyncio.wait_for(self.service.status(), 2.3)
        self.assertFalse(result["available"])
        self.assertEqual(self.exits, [1])
        self.assertEqual(result.get("last_error"), "unavailable")

    async def test_hung_archive_rpc_is_bounded_after_durable_detach(self):
        self.service.archive_timeout_seconds = 0.02
        self.store.set("guild:1:thread:2", "old-thread")
        self.codex.gates["archive"] = asyncio.Event()
        outcome = "unexpected success"
        try:
            await asyncio.wait_for(self.service.archive_scope(1), 0.3)
        except BridgeRequestError as error:
            outcome = error.code
        except TimeoutError:
            outcome = "outer watchdog expired"
        self.assertEqual(outcome, "unavailable")
        self.assertEqual(self.exits, [1])
        self.assertIsNone(ThreadStore(self.store.path).get("guild:1:thread:2"))
        self.assertEqual(self.codex.archived, ["old-thread"])

    async def test_cancelled_archive_rpc_recycles_after_durable_detach(self):
        self.store.set("guild:1:thread:2", "old-thread")
        self.codex.gates["archive"] = asyncio.Event()
        archive = asyncio.create_task(self.service.archive_scope(1))
        self.tasks.append(archive)
        await asyncio.wait_for(self.codex.entered["archive"].wait(), 0.3)
        archive.cancel()
        await asyncio.wait_for(asyncio.gather(archive, return_exceptions=True), 0.3)
        self.assertEqual(self.exits, [1])
        self.assertIsNone(ThreadStore(self.store.path).get("guild:1:thread:2"))
        self.assertEqual(self.codex.archived, ["old-thread"])


class RuntimeWorkerExitTest(unittest.TestCase):
    def run_blocked_runtime(self, phase):
        # This child exists only in the disposable CI runner. Real to_thread workers
        # expose interpreter shutdown hangs that an AsyncMock cannot reproduce.
        script = textwrap.dedent("""
            import asyncio
            import os
            from pathlib import Path
            import sys
            import threading
            from types import SimpleNamespace
            import src.ai.bridge as bridge
            import src.ai.runtime as runtime
            from src.ai.protocol import BridgeRequestError

            phase = sys.argv[2]
            blocker = threading.Event()

            class FakeCodex:
                metadata = SimpleNamespace(serverInfo=SimpleNamespace(version="0.147.0"))

                def __init__(self, _config=None):
                    self.initialized = False

                async def __aenter__(self):
                    if phase == "initialize":
                        await asyncio.to_thread(blocker.wait)
                    self.initialized = True
                    return self

                async def account(self):
                    if not self.initialized:
                        await self.__aenter__()
                    return SimpleNamespace(account=None)

                async def close(self):
                    if phase == "close":
                        await asyncio.to_thread(blocker.wait)

            bridge.AsyncCodex = FakeCodex
            bridge._codex_home = lambda: Path(sys.argv[1])
            bridge._base_instructions = lambda _home: "test-only instructions"
            runtime.SDK_INITIALIZE_TIMEOUT_SECONDS = 0.02
            runtime.SDK_SHUTDOWN_TIMEOUT_SECONDS = 0.02
            os.environ["CODEX_BRIDGE_TOKEN"] = "a" * 64

            async def exercise():
                app = bridge._runtime_app()
                app.freeze()
                await app.startup()
                await app.cleanup()

            asyncio.run(exercise())
        """)
        with tempfile.TemporaryDirectory() as directory:
            child = subprocess.Popen(
                [sys.executable, "-c", script, directory, phase],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                try:
                    stdout, stderr = child.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.communicate(timeout=1)
                    self.fail(f"runtime {phase} left a blocking executor worker alive")
                self.assertEqual(child.returncode, 1, stdout + stderr)
                self.assertIn("Codex", stderr)
                self.assertNotIn("Traceback", stderr)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.communicate(timeout=1)

    def test_startup_deadline_exits_with_real_blocked_worker(self):
        self.run_blocked_runtime("initialize")

    def test_shutdown_deadline_exits_with_real_blocked_worker(self):
        self.run_blocked_runtime("close")


class SdkCollectorCleanupTest(unittest.TestCase):
    def run_sdk_collector(self, mode):
        # Real SDK streams offload router queue.get to the executor. A child
        # watchdog must reap the old implementation when interpreter exit hangs.
        script = textwrap.dedent("""
            import asyncio
            from concurrent.futures import ThreadPoolExecutor
            import os
            from pathlib import Path
            import sys
            import time
            from unittest.mock import Mock, patch

            from aiohttp import ClientSession
            from openai_codex import AsyncCodex, TransportClosedError
            from openai_codex.api import AsyncThread
            from openai_codex.generated.v2_all import (
                TurnCompletedNotification, TurnInterruptResponse, TurnStartResponse,
            )
            from openai_codex.models import Notification
            import src.ai.bridge as bridge
            import src.ai.runtime as runtime
            from src.ai.protocol import BridgeRequestError

            mode = sys.argv[2]

            async def exercise():
                loop = asyncio.get_running_loop()
                # Two real workers make repeated leaks visible without dozens of turns.
                loop.set_default_executor(ThreadPoolExecutor(max_workers=2))
                sdk = AsyncCodex()
                sdk._initialized = True  # Only external initialization/RPCs are synthetic.
                sync = sdk._client._sync
                router = sync._router
                started = []
                resumed = []
                turns = []
                interrupts = []
                interrupt_entered = asyncio.Event()
                interrupted_at = []
                completions = []

                async def start(**options):
                    started.append(options)
                    return AsyncThread(sdk, "thread-1")

                async def resume(thread_id, **options):
                    resumed.append(thread_id)
                    return AsyncThread(sdk, thread_id)

                def turn_start(thread_id, inputs, params=None):
                    turn_id = f"turn-{len(turns) + 1}"
                    turns.append((thread_id, turn_id))
                    return TurnStartResponse.model_validate({
                        "turn": {"id": turn_id, "status": "inProgress", "items": []},
                    })

                def complete(thread_id, turn_id):
                    router.route_notification(Notification(
                        "turn/completed",
                        TurnCompletedNotification.model_validate({
                            "threadId": thread_id,
                            "turn": {"id": turn_id, "status": "interrupted", "items": []},
                        }),
                    ))
                    completions.append(turn_id)

                def interrupt(thread_id, turn_id):
                    interrupts.append(turn_id)
                    interrupted_at.append(time.monotonic())
                    loop.call_soon_threadsafe(interrupt_entered.set)
                    if mode == "cleanup_deadline":
                        # Interrupt uses most of the one budget; completion never arrives.
                        time.sleep(0.14)
                    else:
                        # Real app-server completion may arrive after the interrupt RPC.
                        loop.call_soon_threadsafe(
                            loop.call_later, 0.04, complete, thread_id, turn_id,
                        )
                    return TurnInterruptResponse()

                sdk.thread_start = start
                sdk.thread_resume = resume
                sync.turn_start = turn_start
                sync.turn_interrupt = interrupt
                service = runtime.CodexService(
                    sdk, runtime.ThreadStore(Path(sys.argv[1]) / "threads.json"),
                    timeout_seconds=0.2 if mode == "timeout" else 2,
                )
                service.interrupt_timeout_seconds = 0.2

                if mode == "cleanup_deadline":
                    def fatal_exit(code):
                        print(f"fatal_elapsed={time.monotonic() - interrupted_at[0]:.3f}", flush=True)
                        os._exit(code)
                    service.fatal_exit = fatal_exit

                def queue_get_is_blocked(turn_id):
                    # Observe real frames, without replacing Queue.get, routing, or to_thread.
                    for frame in sys._current_frames().values():
                        in_queue_get = False
                        while frame is not None:
                            if frame.f_code.co_name == "get" and frame.f_code.co_filename.endswith("queue.py"):
                                in_queue_get = True
                            if (
                                in_queue_get
                                and frame.f_code.co_name == "next_turn_notification"
                                and frame.f_code.co_filename.endswith("_message_router.py")
                                and frame.f_locals.get("turn_id") == turn_id
                            ):
                                return True
                            frame = frame.f_back
                    return False

                async def wait_for_queue_get(turn_id):
                    async with asyncio.timeout(1):
                        while not queue_get_is_blocked(turn_id):
                            await asyncio.sleep(0.001)
                    print("real_queue_get_blocked", flush=True)

                runner = http = None
                if mode == "http_cancel":
                    app = bridge.create_app("a" * 64, service)
                    with patch.object(bridge, "_runtime_app", return_value=app), patch.object(
                        bridge.web, "run_app", Mock(),
                    ) as launch, patch.object(sys, "argv", ["codex-bridge", "serve"]):
                        bridge.main()
                    runner = bridge.web.AppRunner(
                        app, handler_cancellation=launch.call_args.kwargs.get("handler_cancellation", False),
                    )
                    await runner.setup()
                    await bridge.web.TCPSite(runner, "127.0.0.1", 0).start()
                    base_url = f"http://127.0.0.1:{runner.addresses[0][1]}"
                    http = ClientSession()

                rounds = 6 if mode == "repeated_cancel" else 1
                try:
                    for index in range(rounds):
                        interrupt_entered.clear()
                        turn_id = f"turn-{index + 1}"
                        if http is not None:
                            job = asyncio.create_task(http.post(base_url + "/v1/chat", json={
                                "conversation_key": "guild:1:thread:2",
                                "text": "private prompt", "images": [],
                            }, headers={"Authorization": "Bearer " + "a" * 64}))
                        else:
                            job = asyncio.create_task(service.chat(
                                "guild:1:thread:2", "private prompt", (),
                            ))
                        await wait_for_queue_get(turn_id)
                        accepted = next(iter(service._admission.jobs))
                        if mode != "timeout":
                            job.cancel()
                            await asyncio.wait_for(interrupt_entered.wait(), 0.5)
                            if mode == "repeated_cancel":
                                accepted.cancel()
                                await asyncio.sleep(0)
                                accepted.cancel()
                        result = (await asyncio.wait_for(
                            asyncio.gather(job, return_exceptions=True), 1,
                        ))[0]
                        if mode == "timeout":
                            assert isinstance(result, BridgeRequestError), repr(result)
                            assert result.code == "timeout", result.code
                        else:
                            assert isinstance(result, asyncio.CancelledError), repr(result)
                        done, _ = await asyncio.wait({accepted}, timeout=1)
                        assert accepted in done, "HTTP handler did not finish SDK cleanup"
                        assert not service._admission.jobs, "request ownership outlived cleanup"
                        # Completion must precede release; sleeping here would hide early release.
                        assert completions == [f"turn-{n + 1}" for n in range(index + 1)], completions
                        assert await asyncio.wait_for(
                            asyncio.to_thread(lambda: "worker reusable"), 0.5,
                        ) == "worker reusable"

                    assert len(started) == 1, "cancelled prompt was retried"
                    assert len(resumed) == rounds - 1
                    assert len(turns) == rounds
                    assert interrupts == [f"turn-{n + 1}" for n in range(rounds)], interrupts
                    assert not router._turn_notifications
                    router.fail_all(TransportClosedError("synthetic shutdown"))
                    await sdk.close()
                    print("scenario_finished", flush=True)
                finally:
                    if http is not None:
                        await http.close()
                    if runner is not None:
                        await runner.cleanup()

            asyncio.run(exercise())
            print("normal_exit", flush=True)
        """)
        with tempfile.TemporaryDirectory() as directory:
            child = subprocess.Popen(
                [sys.executable, "-c", script, directory, mode],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                try:
                    stdout, stderr = child.communicate(timeout=4)
                except subprocess.TimeoutExpired:
                    child.kill()
                    stdout, stderr = child.communicate(timeout=1)
                    stage = "SDK worker hang" if "real_queue_get_blocked" in stdout else "fixture/setup timeout"
                    self.fail(f"{mode}: {stage}\n{stdout}{stderr}")
                if mode == "cleanup_deadline":
                    self.assertEqual(child.returncode, 1, stdout + stderr)
                    self.assertIn("fatal_elapsed=", stdout, stdout + stderr)
                    elapsed = float(stdout.split("fatal_elapsed=", 1)[1].splitlines()[0])
                    self.assertLess(elapsed, 0.27, "interrupt and collector each consumed a separate cleanup budget")
                else:
                    self.assertEqual(child.returncode, 0, stdout + stderr)
                    self.assertIn("normal_exit", stdout)
                self.assertNotIn("Traceback", stderr)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.communicate(timeout=1)

    def test_timeout_drains_real_sdk_stream_and_exits_normally(self):
        self.run_sdk_collector("timeout")

    def test_http_disconnect_drains_real_sdk_stream_and_exits_normally(self):
        self.run_sdk_collector("http_cancel")

    def test_repeated_cancellations_preserve_real_executor_capacity(self):
        self.run_sdk_collector("repeated_cancel")

    def test_missing_terminal_event_uses_one_cleanup_deadline_then_exits(self):
        self.run_sdk_collector("cleanup_deadline")


if __name__ == "__main__":
    unittest.main()

import asyncio
from dataclasses import asdict
import io
import json
import logging
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord
from aiohttp import ClientSession, TCPConnector
from aiohttp.test_utils import TestClient, TestServer
from aiohttp.web_log import AccessLogger
from openai_codex import TransportClosedError

import src.codex_bridge as bridge

from src.admin_panel import AdminPanelView
from src.bot import HoroBot, codex_error_text
from src.codex_bridge import BridgeRequestError, CodexService, ThreadStore, create_app
from src.codex_bridge_client import CodexAccess, CodexBridgeClient, CodexBridgeError, CodexRuntimeStatus
from src.preflight import main as preflight


async def settle():
    # Let runnable tasks reach their event gates; never wait for wall-clock IO.
    for _ in range(12):
        await asyncio.sleep(0)


async def stop_tasks(tasks):
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 1)


class AccessRecoveryTest(unittest.TestCase):
    def test_version_three_empty_roles_never_reactivates_legacy_users(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "access.json"
            path.write_text(json.dumps({
                "version": 3, "guild_id": 10, "channel_ids": [20], "role_ids": [],
            }), encoding="utf-8")
            access = CodexAccess(True, 10, 99, frozenset({30}), state_path=path)
            self.assertFalse(access.allows(10, 20, 30))
            access.set_channels(10, frozenset({20, 21}))
            restarted = CodexAccess(True, 10, 99, frozenset({30}), state_path=path)
            self.assertFalse(restarted.allows(10, 21, 30))

    def test_corrupt_recovery_requires_channels_then_roles(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "access.json"
            path.write_text("{", encoding="utf-8")
            access = CodexAccess(True, 10, 20, frozenset({30}), state_path=path)
            with self.assertRaises(ValueError):
                access.set_roles(10, frozenset({70}))
            access.set_channels(10, frozenset({21}))
            self.assertTrue(access.state_available)
            self.assertFalse(access.allows(10, 21, 30))
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved, {
                "version": 3, "guild_id": 10, "channel_ids": [21], "role_ids": [],
            })
            restarted = CodexAccess(True, 10, 99, frozenset({30}), state_path=path)
            self.assertFalse(restarted.allows(10, 21, 30))
            restarted.set_roles(10, frozenset({70}))
            self.assertTrue(restarted.allows(10, 21, 31, frozenset({70})))
            self.assertFalse(restarted.allows(10, 21, 30))

    def test_unreadable_state_is_not_treated_as_absent_legacy_bootstrap(self):
        with patch.object(Path, "exists", return_value=False), patch.object(
            Path, "read_text", side_effect=PermissionError("state directory denied"),
        ):
            access = CodexAccess(True, 10, 20, frozenset({30}), state_path="/denied/access.json")
        self.assertFalse(access.allows(10, 20, 30))
        self.assertFalse(access.state_available)

    def test_failed_role_persistence_does_not_commit_roles_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "access.json"
            access = CodexAccess(True, 10, 20, frozenset({30}), state_path=path)
            access.set_channels(10, frozenset({20}))
            durable = path.read_bytes()
            with patch("src.codex_bridge_client.os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    access.set_roles(10, frozenset({70}))
            self.assertEqual(path.read_bytes(), durable)
            self.assertTrue(access.allows(10, 20, 30))
            self.assertFalse(access.allows(10, 20, 31, frozenset({70})))

    def test_preflight_empty_roles_reports_denied_roles_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "access.json"
            path.write_text(json.dumps({
                "version": 3, "guild_id": 10, "channel_ids": [20], "role_ids": [],
            }), encoding="utf-8")
            config = SimpleNamespace(
                codex_enabled=True, codex_allowed_guild_id=10,
                codex_allowed_channel_id=20, codex_allowed_user_ids=frozenset({30}),
                temp_voice_enabled=False, steam_free_games_enabled=False,
                ai_text_display_enabled=True,
            )
            output = io.StringIO()
            with patch("src.preflight.AppConfig.from_env", return_value=config), patch("sys.stdout", output):
                preflight(path)
            status = json.loads(output.getvalue())
            self.assertEqual(status["codex_access_mode"], "roles")
            self.assertFalse(status["codex_allowlist_configured"])


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
                with patch("src.codex_bridge.os.replace", side_effect=OSError("disk full")):
                    with self.assertRaises(OSError):
                        if operation == "set":
                            store.set(key, "new-thread", updated_at=2)
                        else:
                            store.pop_many([key])
                self.assertEqual(store.get(key), "old-thread")
                self.assertEqual(path.read_bytes(), durable)
                self.assertEqual(ThreadStore(path).get(key), "old-thread")


class ControlledCodex:
    metadata = SimpleNamespace(serverInfo=SimpleNamespace(version="0.147.0"))

    def __init__(self):
        self.gates = {}
        self.entered = {phase: asyncio.Event() for phase in (
            "account", "start", "resume", "turn", "run", "interrupt", "archive",
        )}
        self.started = []
        self.resumed = []
        self.runs = []
        self.interrupted = []
        self.archived = []
        self.run_error = None
        self.interrupt_error = None

    async def pause(self, phase):
        self.entered[phase].set()
        gate = self.gates.get(phase)
        if gate is not None:
            await gate.wait()

    async def account(self):
        await self.pause("account")
        return SimpleNamespace(account=SimpleNamespace(root=SimpleNamespace(
            plan_type=SimpleNamespace(value="free"),
        )))

    async def thread_start(self, **options):
        self.started.append(options)
        thread_id = f"thread-{len(self.started)}"
        await self.pause("start")
        return ControlledThread(self, thread_id)

    async def thread_resume(self, thread_id, **options):
        self.resumed.append((thread_id, options))
        await self.pause("resume")
        return ControlledThread(self, thread_id)

    async def thread_archive(self, thread_id):
        self.archived.append(thread_id)
        await self.pause("archive")


class ControlledThread:
    def __init__(self, codex, thread_id):
        self.codex = codex
        self.id = thread_id
        self.run_waiter = None
        self.interrupted = False

    async def turn(self, inputs):
        await self.codex.pause("turn")
        return self

    async def run(self):
        self.codex.runs.append(self.id)
        self.run_waiter = asyncio.create_task(self.codex.pause("run"))
        try:
            await self.run_waiter
        except asyncio.CancelledError:
            if not self.interrupted:
                raise
        if self.codex.run_error is not None:
            raise self.codex.run_error
        return SimpleNamespace(final_response="answer")

    async def interrupt(self):
        self.codex.interrupted.append(self.id)
        await self.codex.pause("interrupt")
        if self.codex.interrupt_error is not None:
            raise self.codex.interrupt_error
        # Successful synthetic interrupt terminates this turn only, like the SDK event.
        self.interrupted = True
        if self.run_waiter is not None:
            self.run_waiter.cancel()


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
        task = asyncio.create_task(self.service.chat(key, "Steven", "private prompt", ()))
        self.tasks.append(task)
        return task

    async def bounded_chat_error(self):
        try:
            await asyncio.wait_for(self.service.chat("guild:1:thread:2", "A", "wait", ()), 0.3)
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
        other = await asyncio.wait_for(self.service.chat("guild:2:thread:2", "A", "allowed", ()), 0.3)
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
        with patch("src.codex_bridge.os.replace", side_effect=OSError("mapping write failed")) as replace:
            self.codex.gates["start"].set()
            results = await asyncio.wait_for(asyncio.gather(first, queued, return_exceptions=True), 0.3)
            later = "unexpected success"
            try:
                await asyncio.wait_for(self.service.chat(old_key, "A", "later", ()), 0.3)
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
        reply = await asyncio.wait_for(repaired.chat(old_key, "A", "after repair", ()), 0.3)
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


class StatusService:
    def __init__(self):
        self.status_data = {
            "available": True, "authenticated": True, "plan": "free",
            "sdk_version": "0.147.0", "runtime_version": "0.147.0",
            "web_search": "live", "thread_count": 0,
        }
        self.error = None

    async def status(self):
        return self.status_data

    async def chat(self, *_args):
        if self.error is not None:
            raise self.error
        return "answer"


class SafeHttpLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = StatusService()
        self.token = "a" * 64
        self.http = TestClient(TestServer(create_app(self.token, self.service)))
        await self.http.start_server()
        self.client = CodexBridgeClient(str(self.http.make_url("")).rstrip("/"), self.token)

    async def asyncTearDown(self):
        await asyncio.wait_for(self.client.close(), 1)
        await asyncio.wait_for(self.http.close(), 1)

    async def test_non_ascii_authorization_returns_fixed_401(self):
        response = await self.http.get("/v1/status", headers={"Authorization": "Bearer 非 ASCII"})
        self.assertEqual(response.status, 401)
        self.assertEqual(await response.json(), {"error": "unauthorized"})

    async def test_busy_survives_server_and_client_safe_code_filters(self):
        self.service.error = BridgeRequestError("busy", 429)
        with self.assertRaises(CodexBridgeError) as caught:
            await self.client.chat("guild:1:thread:2", "A", "one", ())
        self.assertEqual(caught.exception.code, "busy")
        self.assertNotEqual(codex_error_text("busy"), codex_error_text("unavailable"))

    async def test_missing_optional_status_fields_default_safely(self):
        status = asdict(await self.client.get_runtime_status())
        self.assertEqual(status.get("active_requests"), 0)
        self.assertEqual(status.get("queued_requests"), 0)
        self.assertIsNone(status.get("last_error"))

    async def test_optional_status_fields_reject_unsafe_counters_and_errors(self):
        self.service.status_data.update({
            "active_requests": True, "queued_requests": -1,
            "last_error": "private-account@example.com raw rpc data",
        })
        status = asdict(await self.client.get_runtime_status())
        self.assertEqual(status.get("active_requests"), 0)
        self.assertEqual(status.get("queued_requests"), 0)
        self.assertNotIn("private-account", repr(status))
        self.assertNotIn("raw rpc", repr(status))
        self.service.status_data.update({"active_requests": 2, "queued_requests": 4, "last_error": "busy"})
        status = asdict(await self.client.get_runtime_status())
        self.assertEqual((status.get("active_requests"), status.get("queued_requests")), (2, 4))
        self.assertEqual(status.get("last_error"), "busy")


class Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class BotAdmissionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = CodexBridgeClient("http://codex:8765", "a" * 64, cooldown_seconds=0)
        self.access = CodexAccess(True, 10, 20, frozenset(range(30, 50)))
        self.access.set_roles(10, frozenset({70}))
        self.bot = HoroBot(
            self.client, self.access, SimpleNamespace(),
            SimpleNamespace(close=AsyncMock()), SimpleNamespace(close=AsyncMock()),
            ai_text_display_enabled=False,
        )
        self.bot._connection.user = SimpleNamespace(id=99)
        self.members = {}
        self.guild = SimpleNamespace(id=10, get_member=self.members.get)
        self.guild.fetch_member = AsyncMock(side_effect=lambda user_id: self.members.get(user_id))
        self.chat_release = asyncio.Event()
        self.chat_entered = asyncio.Event()
        self.chat_calls = []
        self.chat_finished = []
        self.tasks = []
        self.gates = [self.chat_release]
        self.image_reads = []
        self.reply_text = "answer"

        async def request(_method, path, *, payload=None, **_kwargs):
            if path == "/v1/chat":
                self.chat_calls.append(payload)
                self.chat_entered.set()
                try:
                    await self.chat_release.wait()
                    return {"reply": self.reply_text}
                finally:
                    self.chat_finished.append(payload["conversation_key"])
            if path == "/v1/status":
                return StatusService().status_data
            return {}
        self.client._request = request

    async def asyncTearDown(self):
        for gate in self.gates:
            gate.set()
        await stop_tasks(self.tasks)
        await asyncio.wait_for(self.client.close(), 1)
        await asyncio.wait_for(self.bot.close(), 1)

    def message(self, user_id=30, *, thread=False, image_gate=None):
        member = SimpleNamespace(
            id=user_id, bot=False, display_name=f"Member {user_id}", guild=self.guild,
            roles=[SimpleNamespace(id=70)],
        )
        self.members[user_id] = member
        deliveries = []
        output_entered = asyncio.Event()
        channel = SimpleNamespace(
            id=21 if thread else 20, parent_id=20 if thread else None,
            type=discord.ChannelType.public_thread if thread else discord.ChannelType.text,
            typing=Typing, sent=[],
        )
        message = SimpleNamespace(
            author=member, guild=self.guild, channel=channel, webhook_id=None,
            content="<@99> private prompt", attachments=[], reference=None,
            mentions=[SimpleNamespace(id=99)], deliveries=deliveries,
            output_entered=output_entered, output_gate=None, after_output=None,
        )

        async def deliver(content=None, **kwargs):
            output_entered.set()
            if message.output_gate is not None:
                await message.output_gate.wait()
            view = kwargs.get("view")
            text = content if content is not None else repr(view.to_components())
            deliveries.append(text)
            if message.after_output is not None:
                await message.after_output()

        async def send(content=None, **kwargs):
            channel.sent.append(content if content is not None else repr(kwargs["view"].to_components()))
            await deliver(content, **kwargs)

        message.reply = deliver
        channel.send = send
        if image_gate is not None:
            self.gates.append(image_gate)
            async def read():
                self.image_reads.append(user_id)
                await image_gate.wait()
                return b"\x89PNG\r\n\x1a\n"
            message.attachments = [SimpleNamespace(
                filename="one.png", content_type="image/png", size=8, read=read,
            )]
        return message

    def start(self, message):
        task = asyncio.create_task(self.bot.on_message(message))
        self.tasks.append(task)
        return task

    def revoke(self, user_id):
        self.members[user_id] = SimpleNamespace(
            id=user_id, bot=False, roles=[], guild=self.guild, display_name="Member",
        )

    async def test_global_capacity_rejects_seventh_job_before_attachment_download(self):
        image_gate = asyncio.Event()
        image_gate.set()
        messages = [self.message(user_id, image_gate=image_gate) for user_id in range(30, 37)]
        jobs = [self.start(message) for message in messages]
        await asyncio.wait_for(self.chat_entered.wait(), 0.3)
        await settle()
        self.assertEqual(len(self.chat_calls), 2)
        self.assertEqual(self.image_reads, [30, 31])
        self.assertTrue(jobs[6].done(), "seventh accepted job must receive busy immediately")
        self.assertIn(codex_error_text("busy"), messages[6].deliveries)
        self.chat_release.set()
        await asyncio.wait_for(asyncio.gather(*jobs), 0.5)
        self.assertEqual([call["display_name"] for call in self.chat_calls], [f"Member {i}" for i in range(30, 36)])

    async def test_shared_thread_accepts_only_one_additional_waiting_job(self):
        messages = [self.message(user_id, thread=True) for user_id in (30, 31, 32)]
        first = self.start(messages[0])
        await asyncio.wait_for(self.chat_entered.wait(), 0.3)
        second = self.start(messages[1])
        third = self.start(messages[2])
        await settle()
        self.assertTrue(third.done(), "third job for one conversation must be rejected")
        self.assertEqual(len(self.chat_calls), 1)
        self.chat_release.set()
        await asyncio.wait_for(asyncio.gather(first, second, third), 0.3)
        self.assertEqual(len(self.chat_calls), 2)

    async def test_same_key_stays_serialized_through_discord_output(self):
        self.chat_release.set()
        first_message = self.message(30, thread=True)
        first_message.output_gate = asyncio.Event()
        self.gates.append(first_message.output_gate)
        first = self.start(first_message)
        await asyncio.wait_for(first_message.output_entered.wait(), 0.3)
        second = self.start(self.message(31, thread=True))
        await settle()
        self.assertEqual(len(self.chat_calls), 1, "next SDK turn started while prior Discord output was blocked")
        first_message.output_gate.set()
        await asyncio.wait_for(asyncio.gather(first, second), 0.3)
        self.assertEqual(len(self.chat_calls), 2)

    async def test_queued_job_times_out_without_starting_sdk_or_replaying_prompt(self):
        self.client.queue_timeout_seconds = 0.02
        first = self.start(self.message(30, thread=True))
        await asyncio.wait_for(self.chat_entered.wait(), 0.3)
        message = self.message(31, thread=True)
        second = self.start(message)
        done, _ = await asyncio.wait({second}, timeout=0.2)
        self.assertIn(second, done, "queued job exceeded its configured queue deadline")
        self.assertEqual(len(self.chat_calls), 1)
        self.assertIn(codex_error_text("timeout"), message.deliveries)
        self.chat_release.set()
        await asyncio.wait_for(first, 0.3)

    async def test_image_download_has_a_bound_before_sdk_start(self):
        self.client.image_timeout_seconds = 0.02
        image_gate = asyncio.Event()
        message = self.message(image_gate=image_gate)
        job = self.start(message)
        done, _ = await asyncio.wait({job}, timeout=0.2)
        self.assertIn(job, done, "attachment download exceeded its configured deadline")
        self.assertEqual(self.chat_calls, [])
        self.assertNotEqual(message.deliveries, [])

    async def test_overall_deadline_includes_queue_and_image_work(self):
        for stage in ("queue", "images"):
            with self.subTest(stage=stage):
                self.client.work_timeout_seconds = 0.03
                self.client.queue_timeout_seconds = 1
                self.client.image_timeout_seconds = 1
                if stage == "queue":
                    blocker = self.start(self.message(30, thread=True))
                    await asyncio.wait_for(self.chat_entered.wait(), 0.3)
                    job = self.start(self.message(31, thread=True))
                else:
                    job = self.start(self.message(32, image_gate=asyncio.Event()))
                done, _ = await asyncio.wait({job}, timeout=0.2)
                self.assertIn(job, done, f"accepted deadline excluded {stage}")
                await stop_tasks(self.tasks)
                self.chat_entered.clear()

    async def test_overall_deadline_also_bounds_discord_output(self):
        self.client.work_timeout_seconds = 0.02
        self.chat_release.set()
        message = self.message()
        message.output_gate = asyncio.Event()
        self.gates.append(message.output_gate)
        job = self.start(message)
        await asyncio.wait_for(message.output_entered.wait(), 0.3)
        done, _ = await asyncio.wait({job}, timeout=0.2)
        self.assertIn(job, done, "accepted deadline excluded Discord output")
        message.output_gate.set()
        followup = self.message(31)
        await asyncio.wait_for(self.bot.on_message(followup), 0.3)
        self.assertIn("answer", followup.deliveries)

    async def test_role_revoked_during_images_uses_current_member_not_message_snapshot(self):
        self.chat_release.set()
        image_gate = asyncio.Event()
        message = self.message(image_gate=image_gate)
        job = self.start(message)
        await settle()
        self.assertEqual(self.image_reads, [30])
        self.revoke(30)
        image_gate.set()
        await asyncio.wait_for(job, 0.3)
        self.assertEqual(self.chat_calls, [])
        self.assertNotIn("answer", message.deliveries)

    async def test_role_revoked_while_queued_prevents_sdk_start(self):
        first = self.start(self.message(30, thread=True))
        await asyncio.wait_for(self.chat_entered.wait(), 0.3)
        queued_message = self.message(31, thread=True)
        second = self.start(queued_message)
        await settle()
        self.revoke(31)
        self.chat_release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 0.3)
        self.assertEqual(len(self.chat_calls), 1)
        self.assertNotIn("answer", queued_message.deliveries)

    async def test_configuration_generation_change_invalidates_already_accepted_jobs(self):
        first_message = self.message(30, thread=True)
        first = self.start(first_message)
        await asyncio.wait_for(self.chat_entered.wait(), 0.3)
        second_message = self.message(31, thread=True)
        second = self.start(second_message)
        await settle()
        # Both users still have role 70; a new configuration must still invalidate their old work.
        self.access.set_channels(10, frozenset({20, 22}))
        self.chat_release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 0.3)
        self.assertEqual(len(self.chat_calls), 1)
        self.assertNotIn("answer", first_message.deliveries + second_message.deliveries)

    async def test_member_update_cancels_active_revoked_work(self):
        message = self.message()
        job = self.start(message)
        await asyncio.wait_for(self.chat_entered.wait(), 0.3)
        before = message.author
        self.revoke(30)
        await asyncio.wait_for(self.bot.on_member_update(before, self.members[30]), 0.3)
        await settle()
        self.assertTrue(job.done(), "revoked member's running request was not cancelled")
        self.assertNotIn("answer", message.deliveries)

    async def test_fresh_role_check_stops_each_native_and_text_display_output_chunk(self):
        self.chat_release.set()
        self.reply_text = "answer" * 1500
        for display in (False, True):
            for before_first in (False, True):
                with self.subTest(display=display, before_first=before_first):
                    self.bot.ai_text_display_enabled = display
                    message = self.message()
                    if before_first:
                        self.revoke(30)
                        self.guild.fetch_member = AsyncMock(side_effect=[
                            message.author, message.author, self.members[30],
                        ])
                    else:
                        self.guild.fetch_member = AsyncMock(side_effect=self.members.get)
                        async def revoke_after_first():
                            self.revoke(30)
                        message.after_output = revoke_after_first
                    with self.assertLogs(level=logging.INFO) as logs:
                        await asyncio.wait_for(self.bot.on_message(message), 0.3)
                    self.assertEqual(len(message.deliveries), 0 if before_first else 1)
                    self.assertEqual(message.channel.sent, [])
                    rendered = "\n".join(logs.output)
                    self.assertIn("result=unauthorized", rendered)
                    self.assertNotIn("result=success", rendered)

    async def test_text_display_fallback_rechecks_revoked_roles(self):
        self.chat_release.set()
        self.bot.ai_text_display_enabled = True
        message = self.message()
        calls = []
        async def fail_then_revoke(content=None, **kwargs):
            calls.append((content, kwargs))
            if len(calls) == 1:
                self.revoke(30)
                raise discord.HTTPException(SimpleNamespace(status=400, reason="Bad Request"), "private transport data")
        message.reply = fail_then_revoke
        with self.assertLogs(level=logging.INFO) as logs:
            await asyncio.wait_for(self.bot.on_message(message), 0.3)
        self.assertEqual(len(calls), 1, "native fallback emitted AI output after roles were revoked")
        rendered = "\n".join(logs.output)
        self.assertIn("result=unauthorized", rendered)
        self.assertNotIn("result=success", rendered)
        self.assertNotIn("private transport data", rendered)

    async def test_unsent_and_partial_discord_failures_never_log_success(self):
        self.chat_release.set()
        self.reply_text = "private answer" * 600
        for display in (False, True):
            for fail_first in (False, True):
                with self.subTest(display=display, fail_first=fail_first):
                    self.bot.ai_text_display_enabled = display
                    message = self.message()
                    calls = []
                    reply = message.reply
                    async def fail(content=None, **kwargs):
                        calls.append((content, kwargs))
                        raise discord.HTTPException(
                            SimpleNamespace(status=400, reason="Bad Request"),
                            "private transport data",
                        )
                    message.channel.send = fail
                    if fail_first:
                        message.reply = fail
                    else:
                        message.reply = reply
                    with self.assertLogs(level=logging.INFO) as logs:
                        await asyncio.wait_for(self.bot.on_message(message), 0.3)
                    self.assertEqual(len(message.deliveries), 0 if fail_first else 1)
                    self.assertEqual(len(calls), 2 if display else 1)
                    rendered = "\n".join(logs.output)
                    self.assertIn("result=unavailable", rendered)
                    self.assertNotIn("result=success", rendered)
                    for private in ("private prompt", "private answer", "private transport data", "Member 30"):
                        self.assertNotIn(private, rendered)

    async def test_successful_text_display_fallback_logs_success_after_remaining_text(self):
        self.chat_release.set()
        self.bot.ai_text_display_enabled = True
        self.reply_text = "a" * 4000 + "b" * 3000
        for fail_first in (False, True):
            with self.subTest(fail_first=fail_first):
                message = self.message()
                delivered = []
                calls = []
                failed = False
                async def send(content=None, **kwargs):
                    nonlocal failed
                    view = kwargs.get("view")
                    calls.append("display" if view is not None else "native")
                    if view is not None and not failed and (fail_first or delivered):
                        failed = True
                        raise discord.HTTPException(
                            SimpleNamespace(status=400, reason="Bad Request"), "private transport data",
                        )
                    delivered.append(view.children[0].content if view is not None else content)
                message.reply = message.channel.send = send
                with self.assertLogs(level=logging.INFO) as logs:
                    await asyncio.wait_for(self.bot.on_message(message), 0.3)
                self.assertEqual("".join(delivered), "a" * 4000 + "b" * 3000)
                self.assertEqual(
                    calls,
                    ["display", "native", "native", "native", "native"]
                    if fail_first else ["display", "display", "native", "native"],
                )
                self.assertIn("result=success", "\n".join(logs.output))

    async def test_client_close_finishes_cancelled_jobs_before_session_close(self):
        first = self.start(self.message(30, thread=True))
        await asyncio.wait_for(self.chat_entered.wait(), 0.3)
        second = self.start(self.message(31, thread=True))
        await settle()
        pending_at_close = []
        session = SimpleNamespace(closed=False)
        async def close_session():
            pending_at_close.append(sum(not task.done() for task in (first, second)))
            session.closed = True
        session.close = close_session
        self.client._session = session
        await asyncio.wait_for(self.client.close(), 0.3)
        self.assertEqual(pending_at_close, [0])
        self.assertTrue(first.done() and second.done())
        self.assertEqual(len(self.chat_calls), 1)

    async def test_success_logs_only_safe_numeric_lifecycle_timings(self):
        self.chat_release.set()
        message = self.message()
        with self.assertLogs(level=logging.INFO) as logs:
            await asyncio.wait_for(self.bot.on_message(message), 0.3)
        rendered = "\n".join(logs.output)
        self.assertNotIn("private prompt", rendered)
        self.assertNotIn("Member 30", rendered)
        for field in ("queue_ms", "images_ms", "sdk_ms", "discord_ms"):
            self.assertRegex(rendered, rf"\b{field}=[0-9]+(?:\.[0-9]+)?\b")
        self.assertIn("result=success", rendered)

    async def test_role_change_cancels_old_jobs_before_external_archive(self):
        message = self.message()
        job = self.start(message)
        await asyncio.wait_for(self.chat_entered.wait(), 0.3)
        original_request = self.client._request
        active_at_archive = []
        async def request(method, path, **kwargs):
            if path == "/v1/archive":
                active_at_archive.append(not job.done())
                return {}
            return await original_request(method, path, **kwargs)
        self.client._request = request
        view = make_admin(self.access, self.client)
        await asyncio.wait_for(view.handle_codex_role_select(interaction(), (role(80),)), 0.3)
        self.assertEqual(active_at_archive, [False])
        self.assertTrue(job.done())
        self.assertEqual(self.access.role_ids, frozenset({80}))
        self.assertNotIn("answer", message.deliveries)

    async def test_close_and_archive_do_not_cancel_cleanup_twice(self):
        cleanup_entered = asyncio.Event()
        cleanup_release = asyncio.Event()
        self.gates.append(cleanup_release)
        repeated_cancellation = []
        async def request(_method, path, **_kwargs):
            if path != "/v1/chat":
                return {}
            self.chat_entered.set()
            try:
                await self.chat_release.wait()
            except asyncio.CancelledError:
                cleanup_entered.set()
                try:
                    await cleanup_release.wait()
                except asyncio.CancelledError:
                    repeated_cancellation.append(True)
                    raise
                raise
            return {"reply": "answer"}
        self.client._request = request
        job = self.start(self.message())
        await asyncio.wait_for(self.chat_entered.wait(), 0.3)
        closing = asyncio.create_task(self.client.close())
        self.tasks.append(closing)
        await settle()
        self.assertTrue(cleanup_entered.is_set(), "client close never cancelled its running request")
        archiving = asyncio.create_task(self.client.archive_scope(10))
        self.tasks.append(archiving)
        await settle()
        cleanup_release.set()
        await asyncio.wait_for(asyncio.gather(closing, archiving), 0.3)
        self.assertEqual(repeated_cancellation, [])
        self.assertTrue(job.done())


    async def test_accepted_error_output_keeps_thread_ownership_and_can_be_cancelled(self):
        for action in ("close", "archive"):
            with self.subTest(action=action):
                client = CodexBridgeClient("http://codex:8765", "a" * 64, cooldown_seconds=0)
                self.client = self.bot.codex = client
                requests = []
                async def request(_method, path, *, payload=None, **_kwargs):
                    if path == "/v1/chat":
                        requests.append(payload)
                        raise CodexBridgeError("unavailable")
                    return {}
                client._request = request
                first_message = self.message(30, thread=True)
                first_message.output_gate = asyncio.Event()
                self.gates.append(first_message.output_gate)
                first = self.start(first_message)
                second = None
                try:
                    await asyncio.wait_for(first_message.output_entered.wait(), 0.3)
                    second = self.start(self.message(31, thread=True))
                    await settle()
                    with self.subTest(check="same-thread ordering"):
                        self.assertEqual(len(requests), 1, "next turn began while accepted error output was still blocked")
                        self.assertFalse(second.done())
                    if action == "close":
                        await asyncio.wait_for(client.close(), 0.3)
                    else:
                        await asyncio.wait_for(client.archive_scope(10), 0.3)
                    await settle()
                    with self.subTest(check="registered error delivery"):
                        self.assertTrue(first.done(), f"{action} lost track of accepted error delivery")
                        self.assertTrue(second.done())
                finally:
                    first_message.output_gate.set()
                    await stop_tasks([task for task in (first, second) if task is not None])
                    await asyncio.wait_for(client.close(), 0.3)

    async def test_timeout_error_delivery_has_a_bounded_cleanup_budget(self):
        self.client.work_timeout_seconds = 0.02
        self.client.cleanup_timeout_seconds = 0.02
        message = self.message(thread=True)
        message.output_gate = asyncio.Event()
        self.gates.append(message.output_gate)
        job = self.start(message)
        await asyncio.wait_for(message.output_entered.wait(), 0.3)
        done, _ = await asyncio.wait({job}, timeout=0.2)
        self.assertIn(job, done, "timeout notification exceeded its remaining work and cleanup budget")
        self.assertEqual(message.deliveries, [])
        self.assertEqual(len(self.chat_calls), 1)


def make_admin(access, client):
    return AdminPanelView(
        user_id=30, guild_id=10, codex_client=client, codex_access=access,
        codex_status=CodexRuntimeStatus(True, True, "free", "0.147.0", "0.147.0", "live", 0),
        temp_voice=SimpleNamespace(get_guild_status=lambda _guild: SimpleNamespace(
            state_available=True, parent_channel_id=1, child_count=0,
        )),
        steam_free_games=SimpleNamespace(get_guild_status=lambda _guild: SimpleNamespace(
            state_available=True, channel_id=2, active_app_count=0,
            poll_interval_seconds=900, role_ids=(),
        )),
        temp_voice_enabled=False, steam_free_games_enabled=False,
    )


def interaction():
    return SimpleNamespace(
        response=SimpleNamespace(defer=AsyncMock()),
        guild=SimpleNamespace(id=10), user=SimpleNamespace(id=30, roles=[]),
        edit_original_response=AsyncMock(),
    )


def role(role_id):
    return SimpleNamespace(id=role_id, guild=SimpleNamespace(id=10), is_default=lambda: False)


class AdminMutationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.access = CodexAccess(True, 10, 20, frozenset({30}))
        self.access.set_roles(10, frozenset({70}))
        self.client = CodexBridgeClient("http://codex:8765", "a" * 64)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.archives = 0
        self.peak_archives = 0
        self.tasks = []
        async def request(_method, path, **_kwargs):
            if path == "/v1/archive":
                self.archives += 1
                self.peak_archives = max(self.peak_archives, self.archives)
                self.entered.set()
                try:
                    await self.release.wait()
                finally:
                    self.archives -= 1
            return {}
        self.client._request = request

    async def asyncTearDown(self):
        self.release.set()
        await stop_tasks(self.tasks)
        await asyncio.wait_for(self.client.close(), 1)

    async def test_separate_panels_serialize_shared_role_mutations(self):
        first_view = make_admin(self.access, self.client)
        second_view = make_admin(self.access, self.client)
        first = asyncio.create_task(first_view.handle_codex_role_select(interaction(), (role(80),)))
        self.tasks.append(first)
        await asyncio.wait_for(self.entered.wait(), 0.3)
        second = asyncio.create_task(second_view.handle_codex_role_select(interaction(), (role(90),)))
        self.tasks.append(second)
        await settle()
        self.assertEqual(self.peak_archives, 1, "separate panels mutated shared access concurrently")
        self.assertFalse(self.access.allows(10, 20, 30, frozenset({70})))
        self.release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 0.3)
        self.assertEqual(self.access.role_ids, frozenset({90}))

    async def test_channel_change_waits_for_shared_role_mutation(self):
        first_view = make_admin(self.access, self.client)
        second_view = make_admin(self.access, self.client)
        first = asyncio.create_task(first_view.handle_codex_role_select(interaction(), (role(80),)))
        self.tasks.append(first)
        await asyncio.wait_for(self.entered.wait(), 0.3)
        channel = SimpleNamespace(id=21, guild=SimpleNamespace(id=10), type=discord.ChannelType.text)
        second = asyncio.create_task(second_view.handle_codex_channel_select(interaction(), (channel,)))
        self.tasks.append(second)
        await settle()
        self.assertEqual(self.access.channel_ids, frozenset({20}))
        self.release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 0.3)
        self.assertEqual(self.access.channel_ids, frozenset({21}))
        self.assertEqual(self.access.role_ids, frozenset({80}))
        self.assertFalse(self.access.allows(10, 20, 30, frozenset({80})))

    async def test_channels_only_repair_keeps_role_selector_usable_and_access_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "access.json"
            path.write_text("{", encoding="utf-8")
            access = CodexAccess(True, 10, 20, frozenset({30}), state_path=path)
            access.set_channels(10, frozenset({20}))
            view = make_admin(access, self.client)
            overview = repr(view.to_components())
            self.assertIn("0 / 1 已設定", overview)
            self.assertIn("白名單身分組", overview)
            view._render_ai()
            components = view.to_components()
            rendered = repr(components)
            self.assertNotIn("暫用舊使用者白名單", rendered)
            self.assertIn("未允許", rendered)
            selectors = []
            def collect(values):
                for component in values:
                    if component.get("type") == discord.ComponentType.role_select.value:
                        selectors.append(component)
                    collect(component.get("components", []))
            collect(components)
            self.assertEqual(len(selectors), 1)
            self.assertFalse(selectors[0].get("disabled", False))


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
            import src.codex_bridge as bridge

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
            bridge.SDK_INITIALIZE_TIMEOUT_SECONDS = 0.02
            bridge.SDK_SHUTDOWN_TIMEOUT_SECONDS = 0.02
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
            import src.codex_bridge as bridge

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
                service = bridge.CodexService(
                    sdk, bridge.ThreadStore(Path(sys.argv[1]) / "threads.json"),
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
                                "conversation_key": "guild:1:thread:2", "display_name": "A",
                                "text": "private prompt", "images": [],
                            }, headers={"Authorization": "Bearer " + "a" * 64}))
                        else:
                            job = asyncio.create_task(service.chat(
                                "guild:1:thread:2", "A", "private prompt", (),
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
                            assert isinstance(result, bridge.BridgeRequestError), repr(result)
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


class RuntimeHttpSafetyTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def launch_options(app):
        # Exercise the serving entry point, then use its actual aiohttp options
        # against loopback HTTP. No assertion merely checks a keyword exists.
        with patch.object(bridge, "_runtime_app", return_value=app), patch.object(
            bridge.web, "run_app", Mock(),
        ) as launch, patch.object(sys, "argv", ["codex-bridge", "serve"]):
            bridge.main()
        return launch.call_args.kwargs

    async def test_http_disconnect_cancels_and_interrupts_only_the_accepted_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            codex = ControlledCodex()
            codex.gates["run"] = asyncio.Event()
            service = CodexService(codex, ThreadStore(Path(directory) / "threads.json"), timeout_seconds=5)
            exits = []
            service.fatal_exit = exits.append
            app = create_app("a" * 64, service)
            options = self.launch_options(app)
            runner = bridge.web.AppRunner(app, handler_cancellation=options.get("handler_cancellation", False))
            await runner.setup()
            await bridge.web.TCPSite(runner, "127.0.0.1", 0).start()
            base_url = f"http://127.0.0.1:{runner.addresses[0][1]}"
            http = ClientSession()
            request = asyncio.create_task(http.post(base_url + "/v1/chat", json={
                "conversation_key": "guild:1:thread:2", "display_name": "A",
                "text": "private prompt", "images": [],
            }, headers={"Authorization": "Bearer " + "a" * 64}))
            try:
                await asyncio.wait_for(codex.entered["run"].wait(), 0.3)
                request.cancel()
                await asyncio.wait_for(asyncio.gather(request, return_exceptions=True), 0.3)
                try:
                    await asyncio.wait_for(codex.entered["interrupt"].wait(), 0.3)
                except TimeoutError:
                    self.fail("HTTP disconnect did not cancel the SDK turn")
                await settle()
                self.assertEqual(codex.interrupted, ["thread-1"])
                self.assertEqual(len(codex.started), 1)
                self.assertEqual(exits, [])
                status = await asyncio.wait_for(service.status(), 0.3)
                self.assertEqual(status["active_requests"], 0)
                self.assertEqual(status["queued_requests"], 0)
            finally:
                codex.gates["run"].set()
                await stop_tasks([request])
                await asyncio.wait_for(http.close(), 1)
                await asyncio.wait_for(runner.cleanup(), 1)

    async def test_bot_real_http_bridge_and_sdk_keep_shared_thread_output_serialized(self):
        with tempfile.TemporaryDirectory() as directory:
            codex = ControlledCodex()
            store = ThreadStore(Path(directory) / "threads.json")
            service = CodexService(codex, store, timeout_seconds=1)
            exits = []
            service.fatal_exit = exits.append
            http = TestClient(TestServer(create_app("a" * 64, service)))
            await http.start_server()
            client = CodexBridgeClient(str(http.make_url("")).rstrip("/"), "a" * 64, cooldown_seconds=0)
            access = CodexAccess(True, 10, 20, frozenset())
            access.set_roles(10, frozenset({70}))
            bot = HoroBot(client, access, SimpleNamespace(), SimpleNamespace(close=AsyncMock()),
                          SimpleNamespace(close=AsyncMock()), ai_text_display_enabled=False)
            bot._connection.user = SimpleNamespace(id=99)
            members = {}
            guild = SimpleNamespace(id=10, get_member=members.get)
            guild.fetch_member = AsyncMock(side_effect=members.get)
            output_entered = asyncio.Event()
            output_release = asyncio.Event()
            delivered = []
            tasks = []
            def message(user_id):
                author = SimpleNamespace(id=user_id, display_name="Member", bot=False,
                                         roles=[SimpleNamespace(id=70)])
                members[user_id] = author
                async def reply(content=None, **_kwargs):
                    if user_id == 30:
                        output_entered.set()
                        await output_release.wait()
                    delivered.append(content)
                return SimpleNamespace(
                    author=author, guild=guild, webhook_id=None,
                    channel=SimpleNamespace(id=21, parent_id=20, type=discord.ChannelType.public_thread,
                                            typing=Typing, send=AsyncMock()),
                    content="<@99> hello", mentions=[SimpleNamespace(id=99)],
                    attachments=[], reference=None, reply=reply,
                )
            try:
                first = asyncio.create_task(bot.on_message(message(30)))
                tasks.append(first)
                await asyncio.wait_for(output_entered.wait(), 1)
                second = asyncio.create_task(bot.on_message(message(31)))
                tasks.append(second)
                await settle()
                self.assertEqual(codex.runs, ["thread-1"])
                status = await asyncio.wait_for(client.get_runtime_status(), 1)
                self.assertEqual((status.active_requests, status.queued_requests), (0, 0))
                self.assertEqual((status.bot_active_requests, status.bot_queued_requests), (1, 1))
                output_release.set()
                await asyncio.wait_for(asyncio.gather(first, second), 1)
                self.assertEqual(delivered, ["answer", "answer"])
                self.assertEqual(exits, [])
                self.assertEqual(codex.runs, ["thread-1", "thread-1"])
                self.assertEqual(len(codex.started), 1)
                self.assertEqual([entry[0] for entry in codex.resumed], ["thread-1"])
                self.assertEqual(store.get("guild:10:thread:21"), "thread-1")
                status = await asyncio.wait_for(client.get_runtime_status(), 1)
                self.assertEqual((status.bot_active_requests, status.bot_queued_requests), (0, 0))
            finally:
                output_release.set()
                await stop_tasks(tasks)
                await asyncio.wait_for(bot.close(), 1)
                await asyncio.wait_for(http.close(), 1)

    async def test_successful_health_probes_are_quiet_but_failure_and_recovery_are_logged(self):
        service = StatusService()
        app = create_app("a" * 64, service)
        options = self.launch_options(app)
        logger = logging.getLogger("horo.tests.health.access")
        # TestServer discards constructor runner options and forces cancellation.
        # Use the same native runner path as serve so main's options reach HTTP.
        runner = bridge.web.AppRunner(
            app, access_log=logger,
            access_log_class=options.get("access_log_class", AccessLogger),
        )
        await runner.setup()
        await bridge.web.TCPSite(runner, "127.0.0.1", 0).start()
        base_url = f"http://127.0.0.1:{runner.addresses[0][1]}"
        http = ClientSession(connector=TCPConnector(force_close=True))
        async def probe(path="/healthz"):
            response = await http.get(base_url + path)
            await response.read()
            await settle()
            return response
        try:
            with self.assertLogs(level=logging.INFO) as logs:
                first = await probe()
                self.assertEqual(first.status, 200)
                initial = len(logs.output)
                await probe()
                self.assertEqual(len(logs.output), initial, "unchanged successful health probe was logged")
                service.status_data["available"] = False
                failed = await probe()
                self.assertEqual(failed.status, 503)
                self.assertGreater(len(logs.output), initial)
                self.assertIn("503", "\n".join(logs.output))
                failure_logs = len(logs.output)
                service.status_data["available"] = True
                recovered = await probe()
                self.assertEqual(recovered.status, 200)
                self.assertGreater(len(logs.output), failure_logs, "health recovery was hidden")
                recovered_logs = len(logs.output)
                await probe()
                self.assertEqual(len(logs.output), recovered_logs)
                unauthorized = await probe("/v1/status")
                self.assertEqual(unauthorized.status, 401)
                self.assertGreater(len(logs.output), recovered_logs)
                self.assertNotIn("private", "\n".join(logs.output))
        finally:
            await asyncio.wait_for(http.close(), 1)
            await asyncio.wait_for(runner.cleanup(), 1)


if __name__ == "__main__":
    unittest.main()

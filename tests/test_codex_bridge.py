import asyncio
import base64
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from src.codex_bridge import (
    BridgeRequestError,
    _CONFIG_OVERRIDES,
    CodexService,
    ThreadStore,
    _codex_home,
    create_app,
    validate_chat_payload,
)
from src.codex_bridge_client import (
    CodexAccess,
    CodexBridgeClient,
    CodexBridgeError,
    conversation_key,
)


class CodexAccessTest(unittest.TestCase):
    def test_allowlist_requires_exact_guild_channel_and_user(self):
        access = CodexAccess(True, 10, 20, frozenset({30, 40}))

        self.assertTrue(access.allows(10, 20, 30))
        self.assertFalse(access.allows(11, 20, 30))
        self.assertFalse(access.allows(10, 21, 30))
        self.assertFalse(access.allows(10, 20, 31))
        self.assertFalse(CodexAccess(False, 10, 20, frozenset({30})).allows(10, 20, 30))

    def test_conversation_keys_separate_normal_users_and_share_threads(self):
        self.assertEqual(
            conversation_key(10, 20, 30, is_thread=False),
            "guild:10:channel:20:user:30",
        )
        self.assertEqual(
            conversation_key(10, 99, 30, is_thread=True),
            "guild:10:thread:99",
        )


class CodexClientGateTest(unittest.TestCase):
    def test_user_cooldown_is_bounded_and_reusable(self):
        client = CodexBridgeClient(
            "http://codex:8765",
            "a" * 64,
            cooldown_seconds=5,
        )

        self.assertTrue(client.try_start_request(10, now=100))
        self.assertFalse(client.try_start_request(10, now=104.9))
        self.assertTrue(client.try_start_request(10, now=105))

    def test_conversation_lock_is_stable_per_key(self):
        client = CodexBridgeClient("http://codex:8765", "a" * 64)

        first = client.conversation_lock("guild:1:thread:2")

        self.assertIs(first, client.conversation_lock("guild:1:thread:2"))
        self.assertIsNot(first, client.conversation_lock("guild:1:thread:3"))


class ThreadStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "horo_threads.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_mapping_is_versioned_atomic_and_mode_600(self):
        store = ThreadStore(self.path)
        store.set("guild:1:channel:2:user:3", "thread-1", updated_at=123)

        payload = json.loads(self.path.read_text(encoding="utf-8"))

        self.assertEqual(
            payload,
            {
                "version": 1,
                "threads": {
                    "guild:1:channel:2:user:3": {
                        "thread_id": "thread-1",
                        "updated_at": 123,
                    }
                },
            },
        )
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.assertEqual(
            ThreadStore(self.path).get("guild:1:channel:2:user:3"),
            "thread-1",
        )

    def test_corrupt_mapping_fails_closed_without_rewrite(self):
        original = "{not-json"
        self.path.write_text(original, encoding="utf-8")

        with self.assertRaises(ValueError):
            ThreadStore(self.path)

        self.assertEqual(self.path.read_text(encoding="utf-8"), original)


class RuntimePathTest(unittest.TestCase):
    def test_codex_home_is_required_and_fixed(self):
        for value in ("", "/tmp"):
            with self.subTest(value=value):
                with patch.dict("src.codex_bridge.os.environ", {"CODEX_HOME": value}, clear=True):
                    with self.assertRaisesRegex(RuntimeError, "CODEX_HOME"):
                        _codex_home()

    def test_runtime_explicitly_disables_external_tools_and_memories(self):
        required = {
            "agents.enabled=false",
            "features.apps=false",
            "features.goals=false",
            "features.hooks=false",
            "features.memories=false",
            "features.multi_agent=false",
            "features.remote_plugin=false",
            "features.shell_snapshot=false",
            "features.shell_tool=false",
            "features.unified_exec=false",
            "mcp_servers={}",
            'shell_environment_policy.inherit="none"',
        }

        self.assertTrue(required.issubset(set(_CONFIG_OVERRIDES)))


class ChatPayloadTest(unittest.TestCase):
    def test_payload_accepts_bounded_text_and_supported_images(self):
        image = "data:image/png;base64," + base64.b64encode(
            b"\x89PNG\r\n\x1a\n"
        ).decode("ascii")
        payload = validate_chat_payload(
            {
                "conversation_key": "guild:1:thread:2",
                "display_name": " Steven ",
                "text": "",
                "images": [image],
            }
        )

        self.assertEqual(payload.conversation_key, "guild:1:thread:2")
        self.assertEqual(payload.display_name, "Steven")
        self.assertEqual(payload.text, "")
        self.assertEqual(payload.images, (image,))

    def test_payload_rejects_empty_oversized_or_unsupported_input(self):
        invalid = (
            {},
            {
                "conversation_key": "bad",
                "display_name": "Steven",
                "text": "hello",
                "images": [],
            },
            {
                "conversation_key": "guild:1:thread:2",
                "display_name": "Steven",
                "text": "",
                "images": [],
            },
            {
                "conversation_key": "guild:1:thread:2",
                "display_name": "Steven",
                "text": "hello",
                "images": ["data:image/gif;base64,AAAA"],
            },
            {
                "conversation_key": "guild:1:thread:2",
                "display_name": "Steven",
                "text": "hello",
                "images": ["data:image/png;base64,not-base64!"],
            },
            {
                "conversation_key": "guild:1:thread:2",
                "display_name": "Steven",
                "text": "hello",
                "images": ["data:image/png;base64,AAAA"],
            },
            {
                "conversation_key": "guild:1:thread:2",
                "display_name": "Steven",
                "text": "x" * 4001,
                "images": [],
            },
        )
        for payload in invalid:
            with self.subTest(payload=list(payload)):
                with self.assertRaises(BridgeRequestError):
                    validate_chat_payload(payload)


class FakeThread:
    def __init__(self, thread_id, replies):
        self.id = thread_id
        self.replies = replies
        self.calls = []
        self.interrupted = False

    async def run(self, inputs):
        self.calls.append(inputs)
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, asyncio.Event):
            await reply.wait()
        return SimpleNamespace(final_response=reply)

    async def interrupt(self):
        self.interrupted = True


class FakeCodex:
    def __init__(self, replies):
        self.replies = replies
        self.started = []
        self.resumed = []
        self.archived = []
        self.threads = {}

    async def thread_start(self, **kwargs):
        self.started.append(kwargs)
        thread = FakeThread(f"thread-{len(self.started)}", self.replies)
        self.threads[thread.id] = thread
        return thread

    async def thread_resume(self, thread_id, **kwargs):
        self.resumed.append((thread_id, kwargs))
        return self.threads[thread_id]

    async def thread_archive(self, thread_id):
        self.archived.append(thread_id)


class StatusCodex:
    metadata = SimpleNamespace(
        serverInfo=SimpleNamespace(
            version="0.147.0 (Debian 12.0.0; x86_64) unknown (horo_dcb; 0.147.0)"
        )
    )

    async def account(self):
        plan = SimpleNamespace(value="free")
        return SimpleNamespace(account=SimpleNamespace(root=SimpleNamespace(plan_type=plan)))


class ParallelThread:
    def __init__(self, owner, thread_id):
        self.owner = owner
        self.id = thread_id

    async def run(self, _inputs):
        self.owner.active += 1
        self.owner.max_active = max(self.owner.max_active, self.owner.active)
        self.owner.entered += 1
        if self.owner.entered >= self.owner.expected:
            self.owner.ready.set()
        await self.owner.release.wait()
        self.owner.active -= 1
        return SimpleNamespace(final_response="answer")


class ParallelCodex:
    def __init__(self, *, expected):
        self.expected = expected
        self.entered = 0
        self.active = 0
        self.max_active = 0
        self.ready = asyncio.Event()
        self.release = asyncio.Event()
        self.threads = {}

    async def thread_start(self, **_kwargs):
        thread = ParallelThread(self, f"thread-{len(self.threads) + 1}")
        self.threads[thread.id] = thread
        return thread

    async def thread_resume(self, thread_id, **_kwargs):
        return self.threads[thread_id]


class CodexServiceTest(unittest.IsolatedAsyncioTestCase):
    async def test_status_normalizes_runtime_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = CodexService(
                StatusCodex(),
                ThreadStore(Path(temp_dir) / "threads.json"),
            )

            status = await service.status()

        self.assertEqual(status["runtime_version"], "0.147.0")

    async def test_new_conversation_persists_then_resumes_same_thread(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ThreadStore(Path(temp_dir) / "threads.json")
            codex = FakeCodex(["first", "second"])
            service = CodexService(codex, store, timeout_seconds=1)
            key = "guild:1:channel:2:user:3"

            first = await service.chat(key, "Steven", "one", ())
            second = await service.chat(key, "Steven", "two", ())

        self.assertEqual((first, second), ("first", "second"))
        self.assertEqual(store.get(key), "thread-1")
        self.assertEqual(len(codex.started), 1)
        self.assertEqual([item[0] for item in codex.resumed], ["thread-1"])

    async def test_process_restart_resumes_persisted_thread(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "threads.json"
            ThreadStore(path).set(
                "guild:1:channel:2:user:3",
                "thread-existing",
                updated_at=1,
            )
            codex = FakeCodex(["after restart"])
            codex.threads["thread-existing"] = FakeThread(
                "thread-existing",
                codex.replies,
            )
            service = CodexService(codex, ThreadStore(path), timeout_seconds=1)

            reply = await service.chat(
                "guild:1:channel:2:user:3",
                "Steven",
                "resume",
                (),
            )

        self.assertEqual(reply, "after restart")
        self.assertEqual([item[0] for item in codex.resumed], ["thread-existing"])
        self.assertEqual(codex.started, [])

    async def test_sdk_failures_are_normalized_without_raw_details(self):
        cases = (
            ("ChatGPT login required for private-account@example.com", "auth_required", 503),
            ("usage limit exceeded: private quota data", "usage_limit_or_unavailable", 429),
            ("transport failed: private rpc payload", "unavailable", 503),
        )
        for message, code, status in cases:
            with self.subTest(code=code):
                with tempfile.TemporaryDirectory() as temp_dir:
                    service = CodexService(
                        FakeCodex([RuntimeError(message)]),
                        ThreadStore(Path(temp_dir) / "threads.json"),
                        timeout_seconds=1,
                    )

                    with self.assertRaises(BridgeRequestError) as caught:
                        await service.chat(
                            "guild:1:thread:2",
                            "Steven",
                            "hello",
                            (),
                        )

                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.status, status)
                self.assertNotIn("private", str(caught.exception))

    async def test_same_conversation_serializes_while_different_ones_run_in_parallel(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            same_codex = ParallelCodex(expected=1)
            same_service = CodexService(
                same_codex,
                ThreadStore(Path(temp_dir) / "same.json"),
                timeout_seconds=1,
            )
            first = asyncio.create_task(
                same_service.chat("guild:1:thread:2", "A", "one", ())
            )
            await same_codex.ready.wait()
            second = asyncio.create_task(
                same_service.chat("guild:1:thread:2", "B", "two", ())
            )
            await asyncio.sleep(0)

            self.assertEqual(same_codex.active, 1)
            same_codex.release.set()
            await asyncio.gather(first, second)
            self.assertEqual(same_codex.max_active, 1)

            parallel_codex = ParallelCodex(expected=2)
            parallel_service = CodexService(
                parallel_codex,
                ThreadStore(Path(temp_dir) / "parallel.json"),
                timeout_seconds=1,
            )
            tasks = (
                asyncio.create_task(
                    parallel_service.chat(
                        "guild:1:channel:2:user:3",
                        "A",
                        "one",
                        (),
                    )
                ),
                asyncio.create_task(
                    parallel_service.chat(
                        "guild:1:channel:2:user:4",
                        "B",
                        "two",
                        (),
                    )
                ),
            )
            await asyncio.wait_for(parallel_codex.ready.wait(), timeout=1)

            self.assertEqual(parallel_codex.max_active, 2)
            parallel_codex.release.set()
            await asyncio.gather(*tasks)

    async def test_timeout_interrupts_turn_without_retry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ThreadStore(Path(temp_dir) / "threads.json")
            blocker = asyncio.Event()
            codex = FakeCodex([blocker])
            service = CodexService(codex, store, timeout_seconds=0.01)

            with self.assertRaisesRegex(BridgeRequestError, "timeout"):
                await service.chat(
                    "guild:1:thread:2",
                    "Steven",
                    "wait",
                    (),
                )

        self.assertTrue(codex.threads["thread-1"].interrupted)
        self.assertEqual(len(codex.started), 1)

    async def test_archive_scope_removes_mapping_and_archives_threads(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ThreadStore(Path(temp_dir) / "threads.json")
            store.set("guild:1:channel:2:user:3", "thread-a", updated_at=1)
            store.set("guild:1:thread:4", "thread-b", updated_at=1)
            store.set("guild:2:thread:5", "thread-c", updated_at=1)
            codex = FakeCodex([])
            service = CodexService(codex, store, timeout_seconds=1)

            await service.archive_scope(1)

        self.assertEqual(codex.archived, ["thread-a", "thread-b"])
        self.assertIsNone(store.get("guild:1:channel:2:user:3"))
        self.assertEqual(store.get("guild:2:thread:5"), "thread-c")


class FakeBridgeService:
    def __init__(self):
        self.chat_calls = []
        self.archive_calls = []
        self.error = None

    async def status(self):
        return {
            "available": True,
            "authenticated": True,
            "plan": "free",
            "sdk_version": "0.147.0",
            "runtime_version": "0.147.0",
            "web_search": "live",
            "thread_count": 1,
        }

    async def chat(self, key, display_name, text, images):
        if self.error is not None:
            raise self.error
        self.chat_calls.append((key, display_name, text, images))
        return "answer"

    async def archive_scope(self, guild_id, channel_id=None):
        self.archive_calls.append((guild_id, channel_id))


class BridgeHttpTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.token = "d" * 64
        self.service = FakeBridgeService()
        self.http = TestClient(TestServer(create_app(self.token, self.service)))
        await self.http.start_server()
        self.client = CodexBridgeClient(
            str(self.http.make_url("")).rstrip("/"),
            self.token,
        )
        await self.client.start()

    async def asyncTearDown(self):
        await self.client.close()
        await self.http.close()

    async def test_health_status_chat_and_archive_contract(self):
        health = await self.http.get("/healthz")
        unauthorized = await self.http.get("/v1/status")
        status = await self.client.get_runtime_status()
        reply = await self.client.chat(
            "guild:1:channel:2:user:3",
            "Steven",
            "hello",
            (),
        )
        await self.client.archive_scope(1, 2)

        self.assertEqual(health.status, 200)
        self.assertEqual(await health.json(), {"status": "ready"})
        self.assertEqual(unauthorized.status, 401)
        self.assertTrue(status.available)
        self.assertEqual(status.plan, "free")
        self.assertEqual(reply, "answer")
        self.assertEqual(
            self.service.chat_calls,
            [("guild:1:channel:2:user:3", "Steven", "hello", ())],
        )
        self.assertEqual(self.service.archive_calls, [(1, 2)])

    async def test_client_preserves_safe_bridge_error_code(self):
        self.service.error = BridgeRequestError("timeout", 504)

        with self.assertRaises(CodexBridgeError) as caught:
            await self.client.chat(
                "guild:1:thread:2",
                "Steven",
                "hello",
                (),
            )

        self.assertEqual(caught.exception.code, "timeout")

    async def test_invalid_json_and_oversized_body_are_invalid_requests(self):
        headers = {"Authorization": f"Bearer {self.token}"}
        invalid = await self.http.post(
            "/v1/chat",
            data=b"{not-json",
            headers={**headers, "Content-Type": "application/json"},
        )
        oversized = await self.http.post(
            "/v1/chat",
            data=io.BytesIO(b"x" * (24 * 1024 * 1024 + 1)),
            headers={**headers, "Content-Type": "application/json"},
        )

        self.assertEqual(invalid.status, 400)
        self.assertEqual(await invalid.json(), {"error": "invalid_request"})
        self.assertEqual(oversized.status, 400)
        self.assertEqual(await oversized.json(), {"error": "invalid_request"})
        self.assertEqual(self.service.chat_calls, [])

    async def test_unknown_bridge_error_is_not_exposed_by_server_or_client(self):
        self.service.error = BridgeRequestError(
            "private-account@example.com raw rpc payload",
            503,
        )

        with self.assertRaises(CodexBridgeError) as caught:
            await self.client.chat(
                "guild:1:thread:2",
                "Steven",
                "hello",
                (),
            )

        self.assertEqual(caught.exception.code, "unavailable")


if __name__ == "__main__":
    unittest.main()

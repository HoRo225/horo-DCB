import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from src.codex_bridge import (
    BridgeRequestError,
    CodexService,
    ThreadStore,
    validate_chat_payload,
)
from src.codex_bridge_client import CodexAccess, conversation_key


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


class ChatPayloadTest(unittest.TestCase):
    def test_payload_accepts_bounded_text_and_supported_images(self):
        payload = validate_chat_payload(
            {
                "conversation_key": "guild:1:thread:2",
                "display_name": " Steven ",
                "text": "",
                "images": ["data:image/png;base64,AAAA"],
            }
        )

        self.assertEqual(payload.conversation_key, "guild:1:thread:2")
        self.assertEqual(payload.display_name, "Steven")
        self.assertEqual(payload.text, "")
        self.assertEqual(payload.images, ("data:image/png;base64,AAAA",))

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


class CodexServiceTest(unittest.IsolatedAsyncioTestCase):
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


if __name__ == "__main__":
    unittest.main()

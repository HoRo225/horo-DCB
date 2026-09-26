import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from src.ai.admission import Admission
from src.ai.protocol import (
    CodexBridgeError,
    parse_archive_result,
    scope_matches,
    validate_archive_payload,
    validate_chat_payload,
)
from src.ai.thread_store import ThreadStore, migrate_mapping


class ProtocolTests(unittest.TestCase):
    def test_chat_budget_and_parent_validation(self) -> None:
        legacy = validate_chat_payload({
            "conversation_key": "guild:1:thread:3", "text": "hello", "images": [],
        })
        self.assertEqual(legacy.budget_ms, 120000)
        self.assertIsNone(legacy.parent_channel_id)
        payload = {
            "conversation_key": "guild:1:thread:3", "text": "hello", "images": [],
            "budget_ms": 10, "parent_channel_id": 4,
        }
        self.assertEqual(validate_chat_payload(payload).parent_channel_id, 4)
        for invalid in ({**payload, "budget_ms": True}, {**payload, "extra": 1},
                        {**payload, "parent_channel_id": 3},
                        {**payload, "conversation_key": "guild:1:channel:3:user:5"}):
            with self.assertRaises(CodexBridgeError):
                validate_chat_payload(invalid)

    def test_archive_payload_and_result_are_strict(self) -> None:
        self.assertTrue(validate_archive_payload({
            "guild_id": 1, "channel_id": 2, "include_children": True,
        }).include_children)
        for invalid in ({"guild_id": 1, "include_children": True},
                        {"guild_id": 1, "include_children": 1}):
            with self.assertRaises(CodexBridgeError):
                validate_archive_payload(invalid)
        self.assertEqual(parse_archive_result({
            "detached_count": 1, "archived_count": 1,
            "archive_unconfirmed_count": 0,
        }).archived_count, 1)
        with self.assertRaises(ValueError):
            parse_archive_result({"detached_count": True, "archived_count": 0,
                                  "archive_unconfirmed_count": 0})

    def test_scope_matching_includes_unknown_legacy_children(self) -> None:
        self.assertTrue(scope_matches("guild:1:thread:8", 1, 4,
                                      parent_channel_id=None, include_children=True))
        self.assertFalse(scope_matches("guild:1:thread:8", 1, 4,
                                       parent_channel_id=5, include_children=True))
        self.assertTrue(scope_matches("guild:1:channel:4:user:9", 1, 4,
                                      include_children=True))


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_bridge_zero_waiter_capacity_fails_fast(self) -> None:
        admission = Admission(max_waiters=0)
        release = asyncio.Event()

        async def hold(key: str) -> None:
            async with admission.claim(key):
                await release.wait()

        owners = [asyncio.create_task(hold(f"k{i}")) for i in range(2)]
        while len(admission.active_keys) != 2:
            await asyncio.sleep(0)
        with self.assertRaises(CodexBridgeError) as error:
            async with admission.claim("overflow"):
                self.fail("bridge accepted a waiter")
        self.assertEqual(error.exception.code, "busy")
        self.assertFalse(admission.waiting)
        release.set()
        await asyncio.gather(*owners)

    async def test_two_owners_four_waiters_and_busy(self) -> None:
        admission = Admission(max_waiters=4)
        release = asyncio.Event()
        started: set[str] = set()

        async def hold(key: str) -> None:
            async with admission.claim(key):
                started.add(key)
                await release.wait()

        owners = [asyncio.create_task(hold(f"k{i}")) for i in range(2)]
        while len(started) != 2:
            await asyncio.sleep(0)
        waiting = [asyncio.create_task(hold(f"k{i}")) for i in range(2, 6)]
        while len(admission.waiting) != 4:
            await asyncio.sleep(0)
        with self.assertRaises(CodexBridgeError) as error:
            async with admission.claim("k6"):
                self.fail("seventh job unexpectedly admitted")
        self.assertEqual(error.exception.code, "busy")
        release.set()
        await asyncio.gather(*owners, *waiting)

    async def test_queue_deadline_starts_at_acceptance(self) -> None:
        admission = Admission()
        release = asyncio.Event()

        async def hold(key: str) -> None:
            async with admission.claim(key):
                await release.wait()

        async def queue() -> str:
            try:
                async with admission.claim("queued", work_timeout_seconds=0.15):
                    return "admitted"
            except CodexBridgeError as error:
                return error.code

        owners = [asyncio.create_task(hold(f"owner-{index}")) for index in range(2)]
        queued = None
        try:
            while len(admission.active_keys) != 2:
                await asyncio.sleep(0)
            queued = asyncio.create_task(queue())
            for _ in range(100):
                if admission.waiting:
                    break
                await asyncio.sleep(0)
            self.assertEqual(len(admission.waiting), 1)
            job = admission.waiting[0]
            self.assertEqual(job.key, "queued")
            self.assertIsNone(job.started_at)
            self.assertAlmostEqual(job.deadline - job.accepted_at, 0.15)
            self.assertEqual(job.work_deadline, job.deadline - 10)
            self.assertEqual(job.http_deadline, job.deadline - 5)
            self.assertEqual(await queued, "timeout")
            self.assertNotIn(queued, admission.jobs)
            self.assertFalse(admission.waiting)
        finally:
            release.set()
            await asyncio.gather(*owners, return_exceptions=True)
            if queued is not None:
                await asyncio.gather(queued, return_exceptions=True)

    async def test_absolute_cancel_deadline_caps_cleanup_wait(self) -> None:
        admission = Admission()
        entered = asyncio.Event()
        release_cleanup = asyncio.Event()

        async def hold() -> None:
            async with admission.claim("owner"):
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    await release_cleanup.wait()

        owner = asyncio.create_task(hold())
        await entered.wait()
        try:
            with self.assertRaises(CodexBridgeError) as error:
                await asyncio.wait_for(
                    admission.cancel(timeout_seconds=30,
                                     deadline=asyncio.get_running_loop().time() + 0.01),
                    timeout=0.5,
                )
            self.assertEqual(error.exception.code, "unavailable")
            self.assertTrue(owner.cancelling())
        finally:
            release_cleanup.set()
            await asyncio.gather(owner, return_exceptions=True)

    async def test_parent_scope_cancels_only_matching_children(self) -> None:
        admission = Admission()
        release = asyncio.Event()
        cancelled: set[str] = set()

        async def hold(key: str, parent: int) -> None:
            try:
                async with admission.claim(key, parent_channel_id=parent):
                    await release.wait()
            except asyncio.CancelledError:
                cancelled.add(key)
                raise

        first = asyncio.create_task(hold("guild:1:thread:8", 4))
        second = asyncio.create_task(hold("guild:1:thread:9", 5))
        while len(admission.active_keys) != 2:
            await asyncio.sleep(0)
        await admission.cancel(guild_id=1, channel_id=4, include_children=True)
        self.assertEqual(cancelled, {"guild:1:thread:8"})
        release.set()
        await asyncio.gather(first, second, return_exceptions=True)


class ThreadStoreTests(unittest.TestCase):
    def test_v1_upgrade_binding_and_parent_matching(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "threads.json"
            legacy_key = "guild:1:thread:8"
            path.write_text(json.dumps({"version": 1, "threads": {
                legacy_key: {"thread_id": "sdk-8", "updated_at": 12},
                "guild:1:thread:9": {"thread_id": "sdk-9", "updated_at": 13},
            }}), encoding="utf-8")
            store = ThreadStore(path)
            self.assertTrue(store.available)
            self.assertIsNone(store.get_parent(legacy_key))
            store.bind_parent(legacy_key, 4)
            upgraded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(upgraded["version"], 2)
            self.assertEqual(upgraded["threads"][legacy_key], {
                "thread_id": "sdk-8", "updated_at": 12, "parent_channel_id": 4,
            })
            unknown_key = "guild:1:thread:9"
            self.assertEqual(upgraded["threads"][unknown_key], {
                "thread_id": "sdk-9", "updated_at": 13, "parent_channel_id": None,
            })
            with self.assertRaises(ValueError):
                store.bind_parent(legacy_key, 5)
            with self.assertRaises(ValueError):
                store.bind_parent(legacy_key, 8)
            with self.assertRaises(ValueError):
                store.set(legacy_key, "sdk-8", parent_channel_id=5)
            with self.assertRaises(ValueError):
                store.set("guild:1:channel:4:user:7", "sdk-7", parent_channel_id=4)
            self.assertEqual(store.get_parent(legacy_key), 4)
            other_parent_key = "guild:1:thread:10"
            store.set(other_parent_key, "sdk-10", parent_channel_id=6)
            self.assertEqual(store.matching(1, 4, include_children=True), [legacy_key, unknown_key])
            self.assertEqual(store.matching(1, 6, include_children=True), [unknown_key, other_parent_key])
            self.assertEqual(store.pop_many([legacy_key]), ["sdk-8"])

            migrate_mapping(path, 1)
            downgraded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(downgraded["version"], 1)
            self.assertNotIn(legacy_key, downgraded["threads"])
            self.assertEqual(downgraded["threads"][unknown_key], {
                "thread_id": "sdk-9", "updated_at": 13,
            })
            self.assertEqual(downgraded["threads"][other_parent_key]["thread_id"], "sdk-10")

    def test_invalid_migration_preserves_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "threads.json"
            original = '{"version": 2, "threads": {"bad": {}}}'
            path.write_text(original, encoding="utf-8")
            with self.assertRaises(ValueError):
                migrate_mapping(path, 1)
            self.assertEqual(path.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()

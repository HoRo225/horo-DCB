import asyncio
from contextlib import closing
from pathlib import Path
import sqlite3
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

from src.semantic_memory import (
    MemoryScope,
    MemoryVerification,
    SemanticMemory,
    _normalize_vector,
)


class FakeAIClient:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = []

    async def embed(self, inputs, *, model, dimensions):
        self.calls.append((list(inputs), model, dimensions))
        if self.fail:
            raise RuntimeError("embedding unavailable")
        return [self._vector(text, dimensions) for text in inputs]

    @staticmethod
    def _vector(text, dimensions):
        if "牛肉麵" in text or "什麼麵" in text:
            base = [1.0, 0.0, 0.0, 0.0]
        elif "拉麵" in text:
            base = [0.0, 0.0, 1.0, 0.0]
        elif "鍵盤" in text:
            base = [0.0, 1.0, 0.0, 0.0]
        elif "爬山" in text:
            base = [0.0, 0.0, 0.0, 1.0]
        else:
            base = [0.5, 0.5, 0.5, 0.5]
        values = (base * ((dimensions + 3) // 4))[:dimensions]
        return tuple(values)


class PoisonAIClient(FakeAIClient):
    async def embed(self, inputs, *, model, dimensions):
        self.calls.append((list(inputs), model, dimensions))
        if any("poison" in text for text in inputs):
            raise RuntimeError("permanent embedding failure")
        return [self._vector(text, dimensions) for text in inputs]


async def wait_for_row(db_path, message_id, state="ready", timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        with closing(sqlite3.connect(db_path)) as connection:
            row = connection.execute(
                "SELECT state, pending_text, embedding, source_hash, author_name "
                "FROM semantic_messages WHERE message_id=?",
                (message_id,),
            ).fetchone()
        if row is not None and row[0] == state:
            return row
        await asyncio.sleep(0.01)
    raise AssertionError(f"message {message_id} did not reach state {state}")


class SemanticMemoryDatabaseTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db_path = Path(self.tempdir.name) / "semantic.sqlite3"

    async def test_initialize_creates_schema_metadata_and_connection_pragmas(self):
        memory = SemanticMemory(
            FakeAIClient(),
            embedding_model="test-model",
            embedding_dimensions=4,
            db_path=self.db_path,
        )
        memory._initialize_db()

        self.assertEqual(stat.S_IMODE(self.db_path.stat().st_mode), 0o600)
        with closing(memory._connect()) as connection, connection:
            metadata = dict(
                connection.execute(
                    "SELECT key, value FROM semantic_memory_meta"
                ).fetchall()
            )
            indexes = {
                row[1]
                for row in connection.execute("PRAGMA index_list('semantic_messages')")
            }
            failure_indexes = {
                row[1]
                for row in connection.execute(
                    "PRAGMA index_list('semantic_embedding_failures')"
                )
            }
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            synchronous = connection.execute("PRAGMA synchronous").fetchone()[0]
            failure_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='semantic_embedding_failures'"
            ).fetchone()
        self.assertEqual(metadata["schema_version"], "1")
        self.assertEqual(metadata["embedding_model"], "test-model")
        self.assertEqual(metadata["embedding_dimensions"], "4")
        self.assertEqual(journal_mode, "wal")
        self.assertEqual(synchronous, 1)
        self.assertIsNotNone(failure_table)
        self.assertIn("idx_semantic_failure_retry", failure_indexes)
        self.assertIn("idx_semantic_channel_state", indexes)
        self.assertIn("idx_semantic_guild", indexes)

    async def test_metadata_mismatch_fails_closed(self):
        first = SemanticMemory(
            FakeAIClient(),
            embedding_model="model-a",
            embedding_dimensions=4,
            db_path=self.db_path,
        )
        await first.start()
        await first.close()

        second = SemanticMemory(
            FakeAIClient(),
            embedding_model="model-b",
            embedding_dimensions=4,
            db_path=self.db_path,
        )
        await second.start()
        self.assertFalse(second.available)

    async def test_unexpected_worker_failure_marks_memory_unavailable(self):
        memory = SemanticMemory(
            FakeAIClient(),
            embedding_model="test-model",
            embedding_dimensions=4,
            db_path=self.db_path,
        )
        with patch.object(
            memory,
            "_load_pending_sync",
            side_effect=sqlite3.OperationalError("database unavailable"),
        ), self.assertLogs(level="ERROR"):
            await memory.start()
            self.addAsyncCleanup(memory.close)
            for _attempt in range(100):
                if not memory.available:
                    break
                await asyncio.sleep(0.01)

        self.assertFalse(memory.available)

    async def test_capture_becomes_ready_and_drops_pending_text(self):
        client = FakeAIClient()
        memory = SemanticMemory(
            client,
            embedding_model="test-model",
            embedding_dimensions=4,
            db_path=self.db_path,
        )
        await memory.start()
        self.addAsyncCleanup(memory.close)

        await memory.capture_message(
            message_id=1,
            guild_id=100,
            channel_id=200,
            author_name="Alice",
            content="我最喜歡牛肉麵",
            created_at=1_700_000_000,
        )
        row = await wait_for_row(self.db_path, 1)

        self.assertIsNone(row[1])
        self.assertIsNotNone(row[2])
        self.assertEqual(client.calls[0][1:], ("test-model", 4))

    async def test_pending_row_survives_restart_and_is_embedded(self):
        memory = SemanticMemory(
            FakeAIClient(),
            embedding_model="test-model",
            embedding_dimensions=4,
            db_path=self.db_path,
        )
        memory._initialize_db()
        memory._capture_sync(2, 100, 200, "Alice", "我昨天換了鍵盤", 1_700_000_000)

        restarted = SemanticMemory(
            FakeAIClient(),
            embedding_model="test-model",
            embedding_dimensions=4,
            db_path=self.db_path,
        )
        await restarted.start()
        self.addAsyncCleanup(restarted.close)
        row = await wait_for_row(self.db_path, 2)
        self.assertIsNone(row[1])
        self.assertIsNotNone(row[2])

    async def test_stale_embedding_does_not_overwrite_edited_pending_row(self):
        memory = SemanticMemory(
            FakeAIClient(),
            embedding_model="test-model",
            embedding_dimensions=4,
            db_path=self.db_path,
        )
        memory._initialize_db()
        memory._capture_sync(3, 100, 200, "Alice", "我最喜歡牛肉麵", 1_700_000_000)
        old_rows = memory._load_pending_sync()
        memory._update_existing_sync(3, 100, 200, "Alice", "我最喜歡拉麵")
        memory._apply_embeddings_sync(
            old_rows,
            [_normalize_vector((1.0, 0.0, 0.0, 0.0))],
        )

        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            row = connection.execute(
                "SELECT state, pending_text, embedding FROM semantic_messages WHERE message_id=3"
            ).fetchone()
        self.assertEqual(row[0], "pending")
        self.assertEqual(row[1], "我最喜歡拉麵")
        self.assertIsNone(row[2])

    async def test_pending_loader_batches_at_sixteen(self):
        memory = SemanticMemory(
            FakeAIClient(),
            embedding_model="test-model",
            embedding_dimensions=4,
            db_path=self.db_path,
        )
        memory._initialize_db()
        for message_id in range(1, 18):
            memory._capture_sync(
                message_id,
                100,
                200,
                "Alice",
                f"message-{message_id}",
                1_700_000_000 + message_id,
            )

        rows = memory._load_pending_sync()
        self.assertEqual(len(rows), 16)
        self.assertEqual([row["message_id"] for row in rows], list(range(1, 17)))

    async def test_embedding_failure_keeps_pending_row(self):
        memory = SemanticMemory(
            FakeAIClient(fail=True),
            embedding_model="test-model",
            embedding_dimensions=4,
            db_path=self.db_path,
        )
        await memory.start()
        self.addAsyncCleanup(memory.close)
        await memory.capture_message(
            message_id=40,
            guild_id=100,
            channel_id=200,
            author_name="Alice",
            content="暫時無法 Embedding",
            created_at=1_700_000_000,
        )
        await asyncio.sleep(0.05)

        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            row = connection.execute(
                "SELECT state, pending_text, embedding FROM semantic_messages WHERE message_id=40"
            ).fetchone()
        self.assertEqual(row[0], "pending")
        self.assertEqual(row[1], "暫時無法 Embedding")
        self.assertIsNone(row[2])

    async def test_poison_row_does_not_block_later_pending_messages(self):
        memory = SemanticMemory(
            PoisonAIClient(),
            embedding_model="test-model",
            embedding_dimensions=4,
            db_path=self.db_path,
        )
        memory._initialize_db()
        memory._capture_sync(50, 100, 200, "Alice", "poison message", 1_700_000_000)
        memory._capture_sync(51, 100, 200, "Bob", "我昨天換了鍵盤", 1_700_000_001)

        with patch("src.semantic_memory._RETRY_DELAYS", (0.01,)):
            await memory.start()
            try:
                ready = await wait_for_row(self.db_path, 51)
            finally:
                await memory.close()

        self.assertIsNotNone(ready[2])
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            poison = connection.execute(
                "SELECT state, pending_text, embedding "
                "FROM semantic_messages WHERE message_id=50"
            ).fetchone()
            failure = connection.execute(
                "SELECT failure_count FROM semantic_embedding_failures "
                "WHERE message_id=50"
            ).fetchone()
        self.assertEqual(poison[0], "pending")
        self.assertEqual(poison[1], "poison message")
        self.assertIsNone(poison[2])
        self.assertIsNotNone(failure)
        self.assertGreaterEqual(failure[0], 1)

    async def test_deletion_cleanup_runs_even_when_memory_is_unavailable(self):
        memory = SemanticMemory(
            FakeAIClient(),
            embedding_model="test-model",
            embedding_dimensions=4,
            db_path=self.db_path,
        )
        memory._initialize_db()
        memory._capture_sync(60, 100, 200, "Alice", "first", 1_700_000_000)
        memory._capture_sync(61, 100, 201, "Alice", "second", 1_700_000_001)
        memory._capture_sync(62, 100, 202, "Alice", "third", 1_700_000_002)
        memory._capture_sync(63, 101, 203, "Alice", "fourth", 1_700_000_003)
        memory.available = False

        await memory.delete_message(60)
        await memory.delete_messages({61})
        await memory.delete_channel(202)
        await memory.delete_guild(101)

        with closing(sqlite3.connect(self.db_path)) as connection:
            remaining = connection.execute(
                "SELECT message_id FROM semantic_messages ORDER BY message_id"
            ).fetchall()
        self.assertEqual(remaining, [])

    async def test_deleted_row_is_not_resurrected_by_inflight_embedding(self):
        memory = SemanticMemory(
            FakeAIClient(),
            embedding_model="test-model",
            embedding_dimensions=4,
            db_path=self.db_path,
        )
        memory._initialize_db()
        memory.available = True
        memory._capture_sync(41, 100, 200, "Alice", "我最喜歡牛肉麵", 1_700_000_000)
        old_rows = memory._load_pending_sync()
        await memory.delete_message(41)
        memory._apply_embeddings_sync(
            old_rows,
            [_normalize_vector((1.0, 0.0, 0.0, 0.0))],
        )

        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            row = connection.execute(
                "SELECT 1 FROM semantic_messages WHERE message_id=41"
            ).fetchone()
        self.assertIsNone(row)

    async def test_later_delete_waits_for_earlier_capture_mutation(self):
        memory = SemanticMemory(
            FakeAIClient(),
            embedding_model="test-model",
            embedding_dimensions=4,
            db_path=self.db_path,
        )
        memory._initialize_db()
        memory.available = True
        capture_entered = threading.Event()
        release_capture = threading.Event()
        original_capture_sync = memory._capture_sync

        def blocking_capture_sync(*args):
            capture_entered.set()
            self.assertTrue(release_capture.wait(timeout=2.0))
            original_capture_sync(*args)

        with patch.object(memory, "_capture_sync", side_effect=blocking_capture_sync):
            capture_task = asyncio.create_task(
                memory.capture_message(
                    message_id=42,
                    guild_id=100,
                    channel_id=200,
                    author_name="Alice",
                    content="earlier capture",
                    created_at=1_700_000_000,
                )
            )
            self.assertTrue(await asyncio.to_thread(capture_entered.wait, 2.0))
            delete_task = asyncio.create_task(memory.delete_message(42))
            await asyncio.sleep(0)
            release_capture.set()
            await asyncio.gather(capture_task, delete_task)

        with closing(sqlite3.connect(self.db_path)) as connection:
            row = connection.execute(
                "SELECT 1 FROM semantic_messages WHERE message_id=42"
            ).fetchone()
        self.assertIsNone(row)


class SemanticMemorySearchTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "semantic.sqlite3"
        self.client = FakeAIClient()
        self.memory = SemanticMemory(
            self.client,
            embedding_model="test-model",
            embedding_dimensions=4,
            db_path=self.db_path,
        )
        await self.memory.start()

    async def asyncTearDown(self):
        await self.memory.close()
        self.tempdir.cleanup()

    async def add_ready(self, message_id, channel_id, content, author="Alice"):
        await self.memory.capture_message(
            message_id=message_id,
            guild_id=100,
            channel_id=channel_id,
            author_name=author,
            content=content,
            created_at=1_700_000_000 + message_id,
        )
        await wait_for_row(self.db_path, message_id)

    async def test_search_is_channel_scoped_sorted_and_bounded(self):
        await self.add_ready(10, 200, "我最喜歡牛肉麵", "Alice")
        await self.add_ready(11, 200, "我昨天換了鍵盤", "Bob")
        await self.add_ready(12, 201, "我也最喜歡牛肉麵", "Carol")
        current = {
            10: MemoryVerification("current", "我最喜歡牛肉麵", "Alice"),
            11: MemoryVerification("current", "我昨天換了鍵盤", "Bob"),
            12: MemoryVerification("current", "我也最喜歡牛肉麵", "Carol"),
        }

        async def verify(message_id):
            return current[message_id]

        results = await self.memory.search(
            "之前有人喜歡什麼麵？",
            MemoryScope(channel_id=200, verify_message=verify),
        )
        self.assertEqual(results[0]["content"], "我最喜歡牛肉麵")
        self.assertTrue(all(result["author"] != "Carol" for result in results))
        self.assertLessEqual(len(results), 5)
        self.assertNotIn("message_id", results[0])
        self.assertNotIn("channel_id", results[0])
        self.assertNotIn("embedding", results[0])

    async def test_verification_can_fall_through_more_than_eight_candidates(self):
        for message_id in range(100, 108):
            await self.add_ready(message_id, 200, f"我最喜歡牛肉麵 {message_id}")
        await self.add_ready(108, 200, "ordinary lower similarity memory")

        async def verify(message_id):
            if message_id < 108:
                return MemoryVerification("unavailable")
            return MemoryVerification(
                "current",
                "ordinary lower similarity memory",
                "Available",
            )

        results = await self.memory.search(
            "牛肉麵",
            MemoryScope(channel_id=200, verify_message=verify),
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["author"], "Available")
        self.assertEqual(results[0]["content"], "ordinary lower similarity memory")

    async def test_deleted_candidate_is_removed_from_database(self):
        await self.add_ready(20, 200, "我最喜歡牛肉麵")

        async def verify(_message_id):
            return MemoryVerification("deleted")

        results = await self.memory.search(
            "之前有人喜歡什麼麵？",
            MemoryScope(channel_id=200, verify_message=verify),
        )
        self.assertEqual(results, [])
        self.assertFalse(await self.memory.contains_message(20))

    async def test_unavailable_candidate_is_skipped_but_retained(self):
        await self.add_ready(21, 200, "我最喜歡牛肉麵")

        async def verify(_message_id):
            return MemoryVerification("unavailable")

        results = await self.memory.search(
            "之前有人喜歡什麼麵？",
            MemoryScope(channel_id=200, verify_message=verify),
        )
        self.assertEqual(results, [])
        self.assertTrue(await self.memory.contains_message(21))

    async def test_edited_candidate_is_reembedded_and_re_scored(self):
        await self.add_ready(22, 200, "我最喜歡牛肉麵")

        async def verify(_message_id):
            return MemoryVerification("current", "我最喜歡拉麵", "Alice-New")

        results = await self.memory.search(
            "我之前說喜歡拉麵嗎？",
            MemoryScope(channel_id=200, verify_message=verify),
        )
        self.assertEqual(results[0]["content"], "我最喜歡拉麵")
        self.assertEqual(results[0]["author"], "Alice-New")
        self.assertAlmostEqual(results[0]["similarity"], 1.0, places=4)
        self.assertTrue(any(call[0] == ["我最喜歡拉麵"] for call in self.client.calls))
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            row = connection.execute(
                "SELECT author_name, pending_text, state FROM semantic_messages WHERE message_id=22"
            ).fetchone()
        self.assertEqual(row, ("Alice-New", None, "ready"))

    async def test_result_content_is_truncated_to_1000_chars(self):
        text = "牛肉麵" + "x" * 1200
        await self.add_ready(23, 200, text)

        async def verify(_message_id):
            return MemoryVerification("current", text, "Alice")

        results = await self.memory.search(
            "牛肉麵",
            MemoryScope(channel_id=200, verify_message=verify),
        )
        self.assertEqual(len(results[0]["content"]), 1000)

    async def test_delete_message_channel_and_guild(self):
        await self.add_ready(30, 300, "牛肉麵")
        await self.add_ready(31, 301, "鍵盤")
        await self.add_ready(32, 400, "爬山")
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE semantic_messages SET guild_id=200 WHERE message_id=32"
            )

        await self.memory.delete_message(30)
        await self.memory.delete_channel(301)
        await self.memory.delete_guild(200)
        for message_id in (30, 31, 32):
            self.assertFalse(await self.memory.contains_message(message_id))


if __name__ == "__main__":
    unittest.main()

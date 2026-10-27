import unittest

from src.operations import OperationStore
from src.state import open_db, transaction


class OperationsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = open_db(":memory:")
        self.addCleanup(self.db.close)
        self.operations = OperationStore(self.db)

    def test_message_is_bound_to_one_immutable_plan_and_source(self) -> None:
        plan = [{"kind": "game.deal", "inputs": {}}]
        first = self.operations.register_request(10, 1, 2, 3, plan)
        self.assertEqual(first, self.operations.register_request(10, 1, 2, 3, plan))
        plan.append({"kind": "other"})
        for actor, supplied in ((4, first.plan), (1, plan)):
            with self.assertRaises(ValueError):
                self.operations.register_request(10, actor, 2, 3, supplied)
        self.assertEqual(len(self.operations.get_request(10).plan), 1)
        self.assertEqual(self.operations.interrupt_requests(), 1)
        self.assertEqual(self.operations.get_request(10).status, "interrupted")

    def test_receipt_writers_share_the_transaction_and_results_are_immutable(self) -> None:
        with self.assertRaises(RuntimeError):
            self.operations.complete("a", "credit", {}, None)
        with self.assertRaises(ValueError), transaction(self.db):
            self.operations.complete("a", "credit", {}, 2)
            raise ValueError("crash")
        self.assertIsNone(self.operations.get("a"))
        with transaction(self.db):
            self.operations.complete("a", "credit", {}, None)
        self.assertIsNone(self.operations.replay("a", "credit", {}).result)
        with self.assertRaises(ValueError), transaction(self.db):
            self.operations.complete("a", "credit", {}, "changed")
        with self.assertRaises(ValueError):
            self.operations.replay("a", "debit", {})

    def test_running_remote_work_cannot_replay_but_can_finish_once(self) -> None:
        with transaction(self.db):
            self.operations.complete("send", "open_table", {}, {"effect": "send"}, status="running")
        with self.assertRaises(RuntimeError):
            self.operations.replay("send", "open_table", {})
        with transaction(self.db):
            self.operations.complete("send", "open_table", {}, {"message": 10}, resource_id="10")
        self.assertEqual(self.operations.replay("send", "open_table", {}).result, {"message": 10})

    def test_effect_list_reads_once_and_preserves_filters_order_and_payloads(self) -> None:
        with transaction(self.db):
            for identifier, kind in (("b", "voice.create"), ("a", "steam.announce"), ("c", "voice.create")):
                self.operations.create_effect(identifier, kind, 3, {"name": "中文", "ids": [1, 2]},
                                              operation_id="op", nonce=identifier)
            self.operations.update_effect("c", "unknown")
        expected = [self.operations.get_effect(identifier) for identifier in ("b", "a", "c")]
        queries = []
        self.db.set_trace_callback(queries.append)
        try:
            self.assertEqual(self.operations.effects(), expected)
        finally:
            self.db.set_trace_callback(None)
        self.assertEqual(len(queries), 1)
        self.assertEqual(self.operations.effects(kind="voice.create"), [expected[0], expected[2]])
        self.assertEqual(self.operations.effects(status="pending", kind="voice.create"), [expected[0]])
        self.assertEqual(self.operations.effects(status="done"), [])
        self.assertEqual(self.operations.effects(status=("pending", "unknown")), expected)
        self.assertEqual(self.operations.effects(status=("unknown", "done"), kind="voice.create"), [expected[2]])
        self.assertEqual(self.operations.effects(status=()), [])

    def test_pending_items_read_through_the_status_index(self) -> None:
        from src.effects import EffectService
        with transaction(self.db):
            for identifier, status in (("a", "pending"), ("b", "unknown"), ("c", "failed"), ("d", "done")):
                self.operations.create_effect(identifier, "voice.create", 3, {})
                if status in {"unknown", "done"}:
                    self.operations.update_effect(identifier, "unknown")
                if status != "pending":
                    self.operations.update_effect(identifier, status)
        queries = []
        self.db.set_trace_callback(queries.append)
        try:
            pending = EffectService(None, self.db).pending()
        finally:
            self.db.set_trace_callback(None)
        self.assertEqual([effect.effect_id for effect in pending], ["a", "b", "c"])
        [query] = queries
        plan = " ".join(row[3] for row in self.db.execute("EXPLAIN QUERY PLAN " + query))
        self.assertIn("USING INDEX discord_effects_status", plan)

    def test_effect_unknown_requires_reconciliation_before_retry(self) -> None:
        with transaction(self.db):
            effect = self.operations.create_effect("send", "steam.announce", 3, {"winner": 1})
            self.assertEqual(effect.status, "pending")
            self.operations.update_effect("send", "unknown")
        with self.assertRaises(ValueError), transaction(self.db):
            self.operations.update_effect("send", "pending")
        with self.assertRaises(ValueError), transaction(self.db):
            self.operations.create_effect("send", "steam.announce", 3, {"winner": 2})
        with transaction(self.db):
            self.operations.update_effect("send", "pending", retry=True)
            self.operations.update_effect("send", "unknown")
            self.operations.update_effect("send", "done", message_id=12)
        self.assertEqual(self.operations.get_effect("send").message_id, 12)
        self.assertEqual(self.operations.effects(status="pending"), [])
        with self.assertRaises(ValueError), transaction(self.db):
            self.operations.update_effect("send", "pending", retry=True)

    def test_prune_drops_only_finished_records_older_than_thirty_days(self) -> None:
        now = 2_000_000_000
        old, recent = now - 31 * 86400, now - 29 * 86400
        with transaction(self.db):
            for identifier, status, age in (
                ("op-done", "done", old), ("op-skipped", "skipped", old), ("op-recent", "done", recent),
                ("op-running", "running", old), ("op-failed", "failed", old), ("ref-done", "done", old),
                ("ref-pending", "done", old), ("ref-unknown", "skipped", old), ("ref-failed", "done", old),
            ):
                self.operations.complete(identifier, "credit", {}, None, status=status)
                self.db.execute("UPDATE operations SET created_at = ? WHERE operation_id = ?", (age, identifier))
            for identifier, status, age, operation_id in (
                ("e-done", "done", old, "ref-done"), ("e-skipped", "skipped", old, None),
                ("e-recent", "done", recent, None), ("e-pending", "pending", old, "ref-pending"),
                ("e-unknown", "unknown", old, "ref-unknown"), ("e-failed", "failed", old, "ref-failed"),
                ("e-orphan", "pending", old, None),
            ):
                self.operations.create_effect(identifier, "voice.create", 3, {}, operation_id=operation_id)
                if status != "pending":
                    self.operations.update_effect(identifier, "skipped" if status == "skipped" else "unknown")
                if status in {"done", "failed"}:
                    self.operations.update_effect(identifier, status)
                self.db.execute("UPDATE discord_effects SET updated_at = ? WHERE effect_id = ?", (age, identifier))
        for message_id, status, age in ((1, "done", old), (2, "failed", old), (3, "interrupted", old),
                                        (4, "accepted", old), (5, "done", recent)):
            request = self.operations.register_request(message_id, 1, 2, 3, [])
            if status != "accepted":
                self.operations.finish_request(request.request_id, status)
            with transaction(self.db):
                self.db.execute("UPDATE requests SET created_at = ? WHERE message_id = ?", (age, message_id))

        self.assertEqual(self.operations.prune(now), (2, 3, 3))
        self.assertEqual([row[0] for row in self.db.execute("SELECT effect_id FROM discord_effects ORDER BY effect_id")],
                         ["e-failed", "e-orphan", "e-pending", "e-recent", "e-unknown"])
        self.assertEqual([row[0] for row in self.db.execute("SELECT operation_id FROM operations ORDER BY operation_id")],
                         ["op-failed", "op-recent", "op-running", "ref-failed", "ref-pending", "ref-unknown"])
        self.assertEqual([row[0] for row in self.db.execute("SELECT message_id FROM requests ORDER BY message_id")], [4, 5])
        self.assertEqual(self.operations.prune(now), (0, 0, 0))

    def test_midnight_job_prunes_old_operations(self) -> None:
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        from src.games.cog import GamesCog

        with transaction(self.db):
            self.operations.complete("op-done", "credit", {}, None)
            self.db.execute("UPDATE operations SET created_at = unixepoch() - 31 * 86400")
        bot = SimpleNamespace(operations=self.operations)
        with patch("src.games.cog.daily", AsyncMock()) as daily:
            asyncio.run(GamesCog(bot)._prune())
        hour, job = daily.await_args.args
        self.assertEqual(hour, 0)
        with self.assertLogs(level="INFO") as logs:
            asyncio.run(job())
        self.assertIsNone(self.operations.get("op-done"))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM operations").fetchone(), (0,))
        self.assertIn("Pruned 0 effects, 1 operations and 0 requests older than 30 days.", logs.output[-1])


if __name__ == "__main__":
    unittest.main()

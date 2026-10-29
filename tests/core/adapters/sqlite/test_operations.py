import unittest

from src.core.adapters.sqlite.db import open_db, transaction
from src.core.adapters.sqlite.operations import OperationStore


class OperationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = open_db(":memory:")
        self.addCleanup(self.db.close)
        self.operations = OperationStore(self.db)

    def test_results_share_the_callers_transaction_and_are_immutable(self) -> None:
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
        with self.assertRaises(ValueError):
            self.operations.replay("a", "credit", {"amount": 1})

    def test_running_remote_work_cannot_replay_but_can_finish_once(self) -> None:
        with transaction(self.db):
            self.operations.complete("send", "open_table", {}, {"effect": "send"}, status="running")
        with self.assertRaises(RuntimeError):
            self.operations.replay("send", "open_table", {})
        with transaction(self.db):
            self.operations.complete("send", "open_table", {}, {"message": 10}, resource_id="10")
        self.assertEqual(self.operations.replay("send", "open_table", {}).result, {"message": 10})
        self.assertEqual(self.operations.get("send").resource_id, "10")

    def test_missing_operation_id_is_neither_recorded_nor_replayed(self) -> None:
        with transaction(self.db):
            self.operations.complete(None, "credit", {}, None)
        self.assertIsNone(self.operations.replay(None, "credit", {}))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM operations").fetchone(), (0,))

    def test_effect_list_filters_and_keeps_creation_order_and_payloads(self) -> None:
        with transaction(self.db):
            for identifier, kind in (("b", "voice.create"), ("a", "steam.announce"), ("c", "voice.create")):
                self.operations.create_effect(identifier, kind, 3, {"name": "中文", "ids": [1, 2]},
                                              operation_id="op", nonce=identifier)
            self.operations.update_effect("c", "unknown")
        expected = [self.operations.get_effect(identifier) for identifier in ("b", "a", "c")]
        self.assertEqual(expected[0].payload, {"name": "中文", "ids": [1, 2]})
        self.assertEqual(self.operations.effects(), expected)
        self.assertEqual(self.operations.effects(kind="voice.create"), [expected[0], expected[2]])
        self.assertEqual(self.operations.effects(status="pending", kind="voice.create"), [expected[0]])
        self.assertEqual(self.operations.effects(status="done"), [])
        self.assertEqual(self.operations.effects(status=("pending", "unknown")), expected)
        self.assertEqual(self.operations.effects(status=("unknown", "done"), kind="voice.create"), [expected[2]])
        self.assertEqual(self.operations.effects(status=()), [])

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

    def test_only_an_unattempted_effect_can_move_channel(self) -> None:
        with transaction(self.db):
            self.operations.create_effect("send", "steam.announce", 3, {})
            self.assertEqual(self.operations.update_effect("send", "pending", channel_id=4).channel_id, 4)
            self.operations.update_effect("send", "unknown")
        with self.assertRaises(ValueError), transaction(self.db):
            self.operations.update_effect("send", "done", channel_id=5)
        self.assertEqual(self.operations.get_effect("send").channel_id, 4)

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

        self.assertEqual(self.operations.prune(now), (2, 3))
        self.assertEqual([row[0] for row in self.db.execute("SELECT effect_id FROM discord_effects ORDER BY effect_id")],
                         ["e-failed", "e-orphan", "e-pending", "e-recent", "e-unknown"])
        self.assertEqual([row[0] for row in self.db.execute("SELECT operation_id FROM operations ORDER BY operation_id")],
                         ["op-failed", "op-recent", "op-running", "ref-failed", "ref-pending", "ref-unknown"])
        self.assertEqual(self.operations.prune(now), (0, 0))


if __name__ == "__main__":
    unittest.main()

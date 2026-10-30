import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.ai.adapters.sqlite.store import SqliteAiStore
from src.ai.app.ports import StoreError
from src.core.adapters.sqlite.db import open_db
from src.core.adapters.sqlite.operations import OperationStore

MODELS = {"primary": {"model": "claude-sonnet-5-5"}, "fallback": None}


class SqliteAiStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = open_db(":memory:")
        self.addCleanup(self.db.close)
        self.store = SqliteAiStore(self.db)
        self.records = OperationStore(self.db)

    def test_settings_start_from_their_defaults_and_persist(self) -> None:
        store = self.store
        self.assertEqual((store.enabled(), store.channel_ids(), store.role_ids(), store.models()),
                         (False, [], [], None))
        self.assertTrue(store.set_enabled(True, operation_id=None))
        self.assertTrue(store.set_allowlist([10, 11], roles=False, operation_id=None))
        self.assertTrue(store.set_allowlist([20], roles=True, operation_id=None))
        self.assertTrue(store.save_models(MODELS, operation_id=None))
        again = SqliteAiStore(self.db)
        self.assertEqual((again.enabled(), again.channel_ids(), again.role_ids(), again.models()),
                         (True, [10, 11], [20], MODELS))

    def test_operation_receipts_keep_their_kinds_and_inputs(self) -> None:
        self.store.set_enabled(True, operation_id="toggle")
        self.store.set_allowlist([10], roles=False, operation_id="channels")
        self.store.set_allowlist([20], roles=True, operation_id="roles")
        self.store.save_models(MODELS, operation_id="models")
        self.assertEqual(
            {op: (self.records.get(op).kind, self.records.get(op).inputs)
             for op in ("toggle", "channels", "roles", "models")},
            {
                "toggle": ("ai.enabled", {"enabled": True}),
                "channels": ("ai.channel_ids", {"ids": [10]}),
                "roles": ("ai.role_ids", {"ids": [20]}),
                "models": ("ai.models", MODELS),
            },
        )

    def test_replayed_receipt_does_not_overwrite_newer_settings(self) -> None:
        self.assertTrue(self.store.set_allowlist([10], roles=False, operation_id="channels"))
        self.store.set_allowlist([11], roles=False, operation_id=None)
        self.assertFalse(self.store.set_allowlist([10], roles=False, operation_id="channels"))
        self.assertEqual(self.store.channel_ids(), [11])

    def test_corrupt_models_raise_value_error(self) -> None:
        self.store.save_models(MODELS, operation_id=None)
        self.db.execute("UPDATE settings SET value = '{' WHERE key = 'ai.models'")
        with self.assertRaises(ValueError):
            self.store.models()

    def test_tip_is_claimed_once_per_user(self) -> None:
        self.assertEqual([self.store.claim_tip(user) for user in (7, 7, 8)], [True, False, True])

    def test_claimed_tip_survives_a_restart(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "horo.db"
        claims = []
        for _restart in range(2):
            db = open_db(path)
            claims.append(SqliteAiStore(db).claim_tip(7))
            db.close()
        self.assertEqual(claims, [True, False])

    def test_database_failures_surface_as_store_errors(self) -> None:
        self.db.execute("CREATE TRIGGER reject BEFORE INSERT ON settings BEGIN SELECT RAISE(ABORT, 'disk full'); END")
        with self.assertRaises(StoreError) as caught:
            self.store.set_enabled(True, operation_id=None)
        self.assertIsInstance(caught.exception, sqlite3.Error)
        self.assertFalse(self.store.enabled())
        self.db.execute("DROP TABLE settings")
        with self.assertRaises(StoreError):
            self.store.models()
        self.db.execute("DROP TABLE ai_tipped")
        with self.assertRaises(StoreError):
            self.store.claim_tip(7)


if __name__ == "__main__":
    unittest.main()

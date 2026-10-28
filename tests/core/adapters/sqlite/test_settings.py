import unittest

from src.core.adapters.sqlite.db import open_db, transaction
from src.core.adapters.sqlite.operations import OperationStore
from src.core.adapters.sqlite.settings import SettingsStore
from src.core.domain.settings import Setting

ROLES: Setting[list[int]] = Setting("test.roles", [])
CHANNEL: Setting[int | None] = Setting("test.channel", None)
SWITCH = Setting("test.switch", True)


class SettingsStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = open_db(":memory:")
        self.addCleanup(self.db.close)
        self.operations = OperationStore(self.db)
        self.settings = SettingsStore(self.db, self.operations)

    def stored(self) -> dict[str, str]:
        return dict(self.db.execute("SELECT key, value FROM settings"))

    def test_unset_settings_read_as_independent_copies_of_the_default(self) -> None:
        self.assertEqual(self.settings.get(ROLES), [])
        self.assertIsNone(self.settings.get(CHANNEL))
        self.assertIs(self.settings.get(SWITCH), True)
        self.settings.get(ROLES).append(1)
        self.assertEqual(self.settings.get(ROLES), [])
        self.assertEqual(ROLES.default, [])

    def test_put_requires_an_open_transaction(self) -> None:
        with self.assertRaises(RuntimeError):
            self.settings.put({CHANNEL: 1})
        self.assertEqual(self.stored(), {})

    def test_put_overwrites_and_rolls_back_with_its_transaction(self) -> None:
        with transaction(self.db):
            self.settings.put({ROLES: [5, 6], CHANNEL: 1})
        with transaction(self.db):
            self.settings.put({ROLES: [7]})
        with self.assertRaises(ValueError), transaction(self.db):
            self.settings.put({CHANNEL: 2})
            raise ValueError
        self.assertEqual((self.settings.get(ROLES), self.settings.get(CHANNEL)), ([7], 1))

    def test_writes_use_compact_sorted_unicode_json(self) -> None:
        with transaction(self.db):
            self.settings.put({CHANNEL: {"b": [1, 2], "a": "模型"}})
        self.assertEqual(self.stored(), {"test.channel": '{"a":"模型","b":[1,2]}'})

    def test_save_records_the_operation_and_replays_without_writing(self) -> None:
        self.assertTrue(self.settings.save("test.set", {"ids": [5]}, {ROLES: [5]}, operation_id="op"))
        with transaction(self.db):
            self.settings.put({ROLES: [9]})
        self.assertFalse(self.settings.save("test.set", {"ids": [5]}, {ROLES: [5]}, operation_id="op"))
        self.assertEqual(self.settings.get(ROLES), [9])
        self.assertEqual(self.operations.get("op").result, {"ok": True})
        with self.assertRaises(ValueError):
            self.settings.save("test.set", {"ids": [6]}, {ROLES: [6]}, operation_id="op")
        self.assertEqual(self.settings.get(ROLES), [9])

    def test_save_without_operation_id_always_writes(self) -> None:
        self.assertTrue(self.settings.save("test.set", {"ids": [5]}, {ROLES: [5]}, operation_id=None))
        self.assertTrue(self.settings.save("test.set", {"ids": [6]}, {ROLES: [6]}, operation_id=None))
        self.assertEqual(self.settings.get(ROLES), [6])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM operations").fetchone(), (0,))

    def test_save_refuses_empty_values_and_nesting(self) -> None:
        with self.assertRaises(ValueError):
            self.settings.save("test.set", {}, {}, operation_id="op")
        with transaction(self.db), self.assertRaises(RuntimeError):
            self.settings.save("test.set", {}, {CHANNEL: 1}, operation_id="op")
        self.assertEqual(self.stored(), {})
        self.assertIsNone(self.operations.get("op"))


if __name__ == "__main__":
    unittest.main()

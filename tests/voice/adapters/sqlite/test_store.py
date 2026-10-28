import json
import sqlite3
import unittest

from src.core.adapters.sqlite.db import open_db
from src.voice.adapters.sqlite.store import SqliteVoiceStore
from src.voice.app.ports import StoreError
from src.voice.domain.rooms import Overwrite, RoomShape, RoomSpec

OP = "voice.create:abc"
SPEC = RoomSpec(9, 100, 10, RoomShape("房", None, (Overwrite("member", 9, 1, 0),), 0, 64000, None, 1))
INPUTS = json.dumps(SPEC.inputs(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.db = open_db(":memory:")
        self.addCleanup(self.db.close)
        self.store = SqliteVoiceStore(self.db)

    def rows(self, query: str) -> list[tuple]:
        return self.db.execute(query).fetchall()

    def operation(self) -> list[tuple]:
        return self.rows("SELECT operation_id, kind, inputs, status, result, resource_id FROM operations")

    def effects(self) -> list[tuple]:
        return self.rows("SELECT effect_id, operation_id, kind, status, channel_id, payload, error FROM discord_effects")

    def reject(self, table: str, when: str = "INSERT") -> None:
        self.db.execute(f"CREATE TRIGGER reject BEFORE {when} ON {table} BEGIN SELECT RAISE(ABORT, 'disk full'); END")


class SettingTests(StoreTestCase):
    def test_switch_and_entry_persist_under_their_keys_and_operation_kinds(self) -> None:
        self.assertEqual((self.store.enabled(), self.store.entry_id()), (False, None))
        self.assertTrue(self.store.set_enabled(True, operation_id="interaction:1"))
        self.assertFalse(self.store.set_enabled(True, operation_id="interaction:1"))
        self.assertTrue(self.store.bind_entry(10, operation_id="interaction:2"))
        self.assertEqual((self.store.enabled(), self.store.entry_id()), (True, 10))
        self.assertEqual(
            self.rows("SELECT key, value FROM settings ORDER BY key"),
            [("feature.temp_voice", "true"), ("voice.entry_channel_id", "10")],
        )
        self.assertEqual(self.rows("SELECT operation_id, kind, inputs FROM operations ORDER BY rowid"), [
            ("interaction:1", "voice.enabled", '{"enabled":true}'),
            ("interaction:2", "voice.entry", '{"channel_id":10}'),
        ])
        self.store.clear_entry()
        self.assertIsNone(self.store.entry_id())

    def test_unsaved_entry_raises_a_store_error(self) -> None:
        self.reject("settings")
        with self.assertRaises(StoreError):
            self.store.bind_entry(10, operation_id=None)


class CreationTests(StoreTestCase):
    def test_creation_lifecycle_writes_the_operation_effect_and_room(self) -> None:
        self.store.begin_create(OP, SPEC)
        self.assertEqual(self.operation(), [(OP, "voice.create", INPUTS, "running", "null", None)])
        self.assertEqual(self.effects(), [(OP, OP, "voice.create", "unknown", 10, INPUTS, None)])
        self.assertTrue(self.store.creating(9))
        self.store.record_create(OP, SPEC, 50)
        self.assertEqual(self.operation(), [(OP, "voice.create", INPUTS, "done", '{"channel_id":50,"ok":true}', "50")])
        self.assertEqual(self.effects()[0][3], "done")
        self.assertEqual((self.store.rooms(), self.store.room_of(9), self.store.owner_of(50)), ([50], 50, 9))
        self.assertFalse(self.store.creating(9))

    def test_rejected_creation_fails_both_records(self) -> None:
        self.store.begin_create(OP, SPEC)
        self.store.reject_create(OP, SPEC)
        self.assertEqual(self.operation()[0][3:5], ("failed", '{"ok":false}'))
        self.assertEqual(self.effects()[0][3::3], ("failed", "creation_rejected"))

    def test_failed_registration_writes_nothing_and_raises_a_storage_error(self) -> None:
        self.store.begin_create(OP, SPEC)
        self.reject("voice_rooms")
        with self.assertRaises(StoreError) as raised:
            self.store.record_create(OP, SPEC, 50)
        self.assertIsInstance(raised.exception, sqlite3.Error)
        self.assertEqual((self.store.room_count(), self.operation()[0][3], self.effects()[0][3]), (0, "running", "unknown"))

    def test_compensation_records_whether_the_room_was_removed(self) -> None:
        for deleted, operation, effect in ((True, "failed", ("failed", "compensated")),
                                           (False, "running", ("unknown", "registration_and_compensation_failed"))):
            with self.subTest(deleted=deleted):
                self.db.execute("DELETE FROM operations")
                self.db.execute("DELETE FROM discord_effects")
                self.store.begin_create(OP, SPEC)
                self.store.compensate_create(OP, SPEC, 50, deleted=deleted)
                self.assertEqual(self.operation()[0][3:], (
                    operation, json.dumps({"channel_id": 50, "needs_reconciliation": not deleted, "ok": False},
                                          sort_keys=True, separators=(",", ":")), "50",
                ))
                self.assertEqual(self.effects()[0][3::3], effect)

    def test_confirmation_registers_the_room_atomically(self) -> None:
        self.store.begin_create(OP, SPEC)
        effect = self.store.effect(OP)
        self.reject("operations", "UPDATE")
        with self.assertRaises(StoreError):
            self.store.confirm_create(effect, 50, register=True)
        self.assertEqual((self.store.room_count(), self.store.operation(OP).status), (0, "running"))
        self.db.execute("DROP TRIGGER reject")
        self.store.confirm_create(effect, 50, register=True)
        self.assertEqual((self.store.owner_of(50), self.store.operation(OP).resource_id, self.store.effect(OP).status),
                         (9, "50", "done"))


class DeletionTests(StoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store.begin_create(OP, SPEC)
        self.store.record_create(OP, SPEC, 50)

    def status(self) -> str:
        return self.store.effect("voice.delete:50").status

    def test_deletion_is_tracked_under_the_channel_effect_id(self) -> None:
        self.assertEqual(self.store.claim_delete(50, 100, allow_unknown=False), "tracked")
        self.assertEqual(self.effects()[1], ("voice.delete:50", None, "voice.delete", "unknown", 50,
                                             '{"channel_id":50,"guild_id":100}', None))
        self.store.finish_delete(50, tracked=True)
        self.assertEqual((self.status(), self.store.room_count()), ("done", 0))

    def test_an_unknown_deletion_is_held_unless_retried(self) -> None:
        self.store.claim_delete(50, 100, allow_unknown=False)
        self.store.fail_delete(50, "TimeoutError")
        self.assertEqual(self.store.effect("voice.delete:50").error, "TimeoutError")
        self.assertEqual(self.store.claim_delete(50, 100, allow_unknown=False), "held")
        self.assertEqual(self.store.claim_delete(50, 100, allow_unknown=True), "tracked")
        self.assertEqual((self.status(), self.store.room_count()), ("unknown", 1))

    def test_a_finished_deletion_forgets_the_room_and_a_skipped_one_stays(self) -> None:
        self.store.claim_delete(50, 100, allow_unknown=False)
        self.db.execute("UPDATE discord_effects SET status = 'done' WHERE effect_id = 'voice.delete:50'")
        self.assertEqual(self.store.claim_delete(50, 100, allow_unknown=False), "done")
        self.assertEqual(self.store.room_count(), 0)
        self.db.execute("UPDATE discord_effects SET status = 'skipped' WHERE effect_id = 'voice.delete:50'")
        self.assertEqual(self.store.claim_delete(50, 100, allow_unknown=True), "skipped")

    def test_settling_a_failed_deletion_of_a_gone_room(self) -> None:
        self.store.claim_delete(50, 100, allow_unknown=False)
        self.db.execute("UPDATE discord_effects SET status = 'failed' WHERE effect_id = 'voice.delete:50'")
        self.store.settle_delete(self.store.effect("voice.delete:50"))
        self.assertEqual((self.status(), self.store.room_count()), ("done", 0))

    def test_untracked_finish_and_drop_only_forget_the_room(self) -> None:
        self.store.finish_delete(50, tracked=False)
        self.assertIsNone(self.store.effect("voice.delete:50"))
        self.assertFalse(self.store.is_room(50))
        self.store.drop_room(50)


if __name__ == "__main__":
    unittest.main()
